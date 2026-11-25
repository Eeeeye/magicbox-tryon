package tryon;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static java.net.http.HttpRequest.BodyPublishers;

public final class InferenceClient {
    static final String MOCK_RESULT = "MOCK_ONLY_NO_IMAGE_GENERATED";
    private static final int MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
    private final HttpClient http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
        .connectTimeout(Duration.ofSeconds(2)).build();
    private final URI endpoint;
    private final Duration timeout;
    private final boolean real;

    InferenceClient(Config config) {
        endpoint = URI.create(config.inferenceUrl());
        timeout = config.inferenceTimeout();
        real = config.realInference();
    }

    public boolean ready() {
        if (!real) return true;
        var request = HttpRequest.newBuilder(endpoint.resolve("/ready")).timeout(Duration.ofSeconds(2)).GET().build();
        try {
            return http.send(request, HttpResponse.BodyHandlers.discarding()).statusCode() == 200;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        } catch (IOException e) {
            return false;
        }
    }

    public byte[] infer(ImageStorage.StagedImages images, String category) throws IOException, InterruptedException {
        String boundary = "tryon-" + UUID.randomUUID();
        var body = BodyPublishers.concat(part(boundary, images.person()), part(boundary, images.garment()),
            BodyPublishers.ofString("--" + boundary + "--\r\n"));
        var request = HttpRequest.newBuilder(endpoint).timeout(timeout)
            .header("Content-Type", "multipart/form-data; boundary=" + boundary)
            .header("Accept", real ? "image/png" : "text/plain")
            .header("X-Request-Id", images.id()).header("X-Garment-Category", category).POST(body).build();
        var response = exchange(request);
        if (response.statusCode() == 422) {
            String message = new String(response.body(), StandardCharsets.UTF_8);
            throw new InferenceFailure(message.contains("GARMENT_REGION_NOT_FOUND")
                ? "GARMENT_REGION_NOT_FOUND" : "INFERENCE_INPUT_REJECTED");
        }
        if (real && response.statusCode() == 507) throw new InferenceFailure("MODEL_MEMORY_LIMIT");
        if (real && response.statusCode() == 503) throw new InferenceFailure("INFERENCE_BUSY");
        if (response.statusCode() != 200) throw new InferenceFailure("INFERENCE_HTTP_ERROR");
        if (!real && !MOCK_RESULT.equals(new String(response.body(), StandardCharsets.UTF_8)))
            throw new InferenceFailure("INVALID_INFERENCE_RESPONSE");
        if (!matches(response, "X-Person-SHA256", images.person().sha256())
                || !matches(response, "X-Garment-SHA256", images.garment().sha256()))
            throw new InferenceFailure("INFERENCE_INPUT_HASH_MISMATCH");
        if (real && (!matches(response, "X-Request-Id", images.id())
                || !matches(response, "Content-Type", "image/png")))
            throw new InferenceFailure("INVALID_INFERENCE_RESPONSE");
        return response.body();
    }

    private static boolean matches(HttpResponse<?> response, String header, String expected) {
        return response.headers().firstValue(header).orElse("").equals(expected);
    }

    private static HttpRequest.BodyPublisher part(String boundary, ImageMeta meta) throws IOException {
        String filename = meta.role() + (meta.mimeType().equals("image/png") ? ".png" : ".jpg");
        String header = "--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + meta.role()
            + "\"; filename=\"" + filename + "\"\r\nContent-Type: " + meta.mimeType() + "\r\n\r\n";
        return BodyPublishers.concat(BodyPublishers.ofString(header), BodyPublishers.ofFile(Path.of(meta.path())),
            BodyPublishers.ofString("\r\n"));
    }

    private HttpResponse<byte[]> exchange(HttpRequest request) throws IOException, InterruptedException {
        var call = http.sendAsync(request, info -> new LimitedBytes());
        try {
            return call.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            call.cancel(true);
            throw new InferenceFailure("INFERENCE_TIMEOUT");
        } catch (InterruptedException e) {
            call.cancel(true);
            throw e;
        } catch (ExecutionException e) {
            if (e.getCause() instanceof java.net.http.HttpTimeoutException) throw new InferenceFailure("INFERENCE_TIMEOUT");
            if (e.getCause() instanceof InferenceFailure failure) throw failure;
            throw new InferenceFailure("INFERENCE_UNAVAILABLE");
        }
    }

    public String inferLegacy(String url) {
        var request = HttpRequest.newBuilder(URI.create(url)).timeout(timeout)
            .header("Content-Type", "application/json").POST(BodyPublishers.ofString("{\"mode\":\"mock\"}")).build();
        try {
            var response = exchange(request);
            if (response.statusCode() != 200) return "INFERENCE_HTTP_ERROR";
            return MOCK_RESULT.equals(new String(response.body(), StandardCharsets.UTF_8)) ? "" : "INVALID_INFERENCE_RESPONSE";
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return "INTERRUPTED";
        } catch (IOException e) {
            return e.getMessage();
        }
    }

    static final class InferenceFailure extends IOException {
        InferenceFailure(String code) { super(code); }
    }

    private static final class LimitedBytes implements HttpResponse.BodySubscriber<byte[]> {
        private final HttpResponse.BodySubscriber<byte[]> delegate = HttpResponse.BodySubscribers.ofByteArray();
        private Flow.Subscription subscription;
        private long bytes;
        public CompletionStage<byte[]> getBody() { return delegate.getBody(); }
        public void onSubscribe(Flow.Subscription value) { subscription = value; delegate.onSubscribe(value); }
        public void onNext(List<ByteBuffer> buffers) {
            for (ByteBuffer buffer : buffers) bytes += buffer.remaining();
            if (bytes > MAX_RESPONSE_BYTES) {
                subscription.cancel();
                delegate.onError(new InferenceFailure("INFERENCE_RESPONSE_TOO_LARGE"));
            } else {
                delegate.onNext(buffers);
            }
        }
        public void onError(Throwable error) { delegate.onError(error); }
        public void onComplete() { delegate.onComplete(); }
    }
}
