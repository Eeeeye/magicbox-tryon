package tryon;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.channels.Channels;
import java.nio.channels.SeekableByteChannel;
import java.nio.file.NoSuchFileException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.Map;
import java.util.stream.Collectors;

public final class TryOnHttpHandler implements HttpHandler {
    private static final System.Logger LOG = System.getLogger(TryOnHttpHandler.class.getName());
    private static final String TASKS = "/api/tryon/tasks";
    private static final String DEMO = "/api/demo/tasks";
    private record Asset(String file, String mime) {}
    private static final Map<String, Asset> ASSETS = Map.of(
        "/", new Asset("web/index.html", "text/html; charset=utf-8"),
        "/app.js", new Asset("web/app.js", "application/javascript; charset=utf-8"),
        "/ui-state.js", new Asset("web/ui-state.js", "application/javascript; charset=utf-8"),
        "/style.css", new Asset("web/style.css", "text/css; charset=utf-8"),
        "/samples/person.jpg", new Asset("samples/person.jpg", "image/jpeg"),
        "/samples/person-lower.png", new Asset("samples/person-lower.png", "image/png"),
        "/samples/garment.jpg", new Asset("samples/garment.jpg", "image/jpeg"),
        "/samples/lower.jpg", new Asset("samples/lower.jpg", "image/jpeg"),
        "/samples/dress.jpg", new Asset("samples/dress.jpg", "image/jpeg")
    );
    private final Config config;
    private final TryOnService service;

    public TryOnHttpHandler(Config config, TryOnService service) {
        this.config = config;
        this.service = service;
    }

    @Override public void handle(HttpExchange exchange) throws IOException {
        exchange.getResponseHeaders().set("X-Content-Type-Options", "nosniff");
        exchange.getResponseHeaders().set("Cache-Control", "private, no-store");
        try {
            String path = exchange.getRequestURI().getPath();
            switch (exchange.getRequestMethod()) {
                case "GET" -> get(exchange, path);
                case "POST" -> post(exchange, path);
                default -> reply(exchange, 405, error("METHOD_NOT_ALLOWED"));
            }
        } catch (MultipartParser.PayloadTooLargeException | ImageValidator.PayloadTooLargeException e) {
            reply(exchange, 413, error(e.getMessage()));
        } catch (MultipartParser.BadRequestException | ImageValidator.ValidationException e) {
            reply(exchange, 400, error(e.getMessage()));
        } catch (TryOnService.ConflictException e) {
            reply(exchange, 409, error(e.getMessage()));
        } catch (TryOnService.BusyException e) {
            exchange.getResponseHeaders().set("Retry-After", "5");
            reply(exchange, 503, error(e.getMessage()));
        } catch (Exception e) {
            LOG.log(System.Logger.Level.ERROR, "Request failed", e);
            if (exchange.getResponseCode() < 0) reply(exchange, 500, error("INTERNAL_ERROR"));
        } finally {
            exchange.close();
        }
    }

    private void get(HttpExchange exchange, String path) throws IOException {
        if (path.equals("/health") || path.equals("/health/live")) {
            health(exchange);
        } else if (path.equals("/health/ready")) {
            boolean ready = service.isReady();
            reply(exchange, ready ? 200 : 503, "{\"status\":" + quote(ready ? "READY" : "NOT_READY")
                + ",\"scope\":" + quote(config.realInference() ? "pipeline" : "worker") + "}");
        } else if (ASSETS.containsKey(path)) {
            Asset asset = ASSETS.get(path);
            file(exchange, Path.of(asset.file()), asset.mime());
        } else if (path.equals(TASKS)) {
            int limit = queryLimit(exchange.getRequestURI().getRawQuery());
            String tasks = service.latest(limit).stream().map(TryOnHttpHandler::taskJson).collect(Collectors.joining(","));
            reply(exchange, 200, "{\"tasks\":[" + tasks + "],\"count\":" + service.repository().size() + ",\"limit\":" + limit + "}");
        } else if (path.startsWith(TASKS + "/")) {
            String suffix = path.substring(TASKS.length() + 1);
            boolean wantsResult = suffix.endsWith("/result");
            String id = wantsResult ? suffix.substring(0, suffix.length() - "/result".length()) : suffix;
            var found = service.find(id);
            if (found.isEmpty()) reply(exchange, 404, error("TASK_NOT_FOUND"));
            else if (wantsResult) result(exchange, found.get());
            else reply(exchange, 200, taskJson(found.get()));
        } else if (!config.realInference() && path.startsWith(DEMO + "/")) {
            var found = service.findDemo(path.substring(DEMO.length() + 1));
            if (found.isEmpty()) reply(exchange, 404, error("TASK_NOT_FOUND"));
            else reply(exchange, 200, demoJson(found.get()));
        } else {
            reply(exchange, 404, error("NOT_FOUND"));
        }
    }

    private void post(HttpExchange exchange, String path) throws IOException {
        if (path.equals(TASKS)) {
            var parts = MultipartParser.parse(exchange.getRequestBody(), exchange.getRequestHeaders().getFirst("Content-Type"), config.maxBodyBytes());
            String category = exchange.getRequestHeaders().getFirst("X-Garment-Category");
            Task task = service.submit(parts, category == null ? "upper" : category,
                exchange.getRequestHeaders().getFirst("Idempotency-Key"));
            exchange.getResponseHeaders().set("Location", TASKS + "/" + task.id());
            reply(exchange, 202, taskJson(task));
        } else if (!config.realInference() && path.equals(DEMO)) {
            if (exchange.getRequestBody().read() != -1) reply(exchange, 400, error("DEMO_REQUIRES_EMPTY_BODY"));
            else {
                var task = service.submitDemo();
                reply(exchange, 202, "{\"taskId\":" + quote(task.id()) + ",\"status\":\"QUEUED\",\"mode\":\"mock\",\"error\":\"\",\"result\":null}");
            }
        } else {
            reply(exchange, 404, error("NOT_FOUND"));
        }
    }

    private void result(HttpExchange exchange, Task task) throws IOException {
        if (task.status() != TaskStatus.SUCCEEDED) {
            reply(exchange, 409, error("RESULT_NOT_READY"));
        } else if (!task.mode().equals("catvton")) {
            reply(exchange, 409, error("MOCK_RESULT_NOT_IMAGE"));
        } else {
            Path result = config.dataDirectory().resolve(task.id()).resolve("result.png");
            try (var channel = Files.newByteChannel(result)) {
                exchange.getResponseHeaders().set("Content-Disposition", "inline; filename=\"tryon-" + task.id() + ".png\"");
                sendFile(exchange, channel, "image/png");
            } catch (NoSuchFileException missing) {
                if (service.find(task.id()).isEmpty()) reply(exchange, 404, error("TASK_NOT_FOUND"));
                else reply(exchange, 500, error("STORED_RESULT_MISSING"));
            }
        }
    }

    private static void file(HttpExchange exchange, Path path, String mime) throws IOException {
        try (var channel = Files.newByteChannel(path)) {
            sendFile(exchange, channel, mime);
        } catch (NoSuchFileException missing) {
            reply(exchange, 404, error("FILE_NOT_FOUND"));
        }
    }

    private static void sendFile(HttpExchange exchange, SeekableByteChannel channel, String mime) throws IOException {
        // Read length and bytes from the same open file, even if its task expires mid-download.
        exchange.getResponseHeaders().set("Content-Type", mime);
        exchange.sendResponseHeaders(200, channel.size());
        Channels.newInputStream(channel).transferTo(exchange.getResponseBody());
    }

    private void health(HttpExchange exchange) throws IOException {
        String counts = service.statusCounts().entrySet().stream()
            .map(entry -> quote(entry.getKey().name()) + ":" + entry.getValue()).collect(Collectors.joining(","));
        reply(exchange, 200, "{\"status\":\"UP\",\"scope\":\"process\",\"mode\":" + quote(config.mode())
            + ",\"taskCount\":" + service.repository().size() + ",\"queueDepth\":" + service.queueDepth()
            + ",\"activeWorkers\":" + service.activeWorkers() + ",\"tasksByStatus\":{" + counts + "}}");
    }

    private static String demoJson(TryOnService.DemoTask task) {
        return "{\"taskId\":" + quote(task.id()) + ",\"status\":" + quote(task.status().name())
            + ",\"mode\":\"mock\",\"error\":" + quote(task.error()) + ",\"result\":" + quote(task.result()) + "}";
    }

    private static String taskJson(Task task) {
        var s = task.snapshot();
        return "{\"taskId\":" + quote(task.id()) + ",\"status\":" + quote(s.status().name())
            + ",\"mode\":" + quote(task.mode()) + ",\"category\":" + quote(task.category())
            + ",\"error\":" + quote(s.error()) + ",\"result\":" + quote(s.result())
            + ",\"durationMs\":" + s.durationMs() + ",\"createdAt\":" + instant(task.createdAt())
            + ",\"updatedAt\":" + instant(s.updatedAt()) + ",\"timeline\":{\"queuedAt\":" + instant(s.queuedAt())
            + ",\"runningAt\":" + instant(s.runningAt()) + ",\"completedAt\":" + instant(s.completedAt())
            + "},\"inputs\":{\"person\":" + imageJson(task.person()) + ",\"garment\":" + imageJson(task.garment()) + "}}";
    }

    private static String imageJson(ImageMeta meta) {
        return "{\"mimeType\":" + quote(meta.mimeType()) + ",\"bytes\":" + meta.bytes() + ",\"width\":" + meta.width()
            + ",\"height\":" + meta.height() + ",\"sha256\":" + quote(meta.sha256()) + "}";
    }

    private static String instant(Instant value) { return value == null ? "null" : quote(value.toString()); }

    private static int queryLimit(String raw) throws MultipartParser.BadRequestException {
        if (raw == null || raw.isBlank()) return 20;
        for (String item : raw.split("&")) {
            if (item.startsWith("limit=")) {
                try {
                    int limit = Integer.parseInt(item.substring("limit=".length()));
                    if (limit >= 1 && limit <= 100) return limit;
                } catch (NumberFormatException ignored) { /* handled as a bad limit below */ }
                break;
            }
        }
        throw new MultipartParser.BadRequestException("INVALID_LIMIT");
    }

    private static String error(String message) { return "{\"error\":" + quote(message) + "}"; }

    // All public strings are server-generated codes, timestamps, hashes or local route paths.
    private static String quote(String value) {
        if (value == null) return "null";
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\r", "\\r").replace("\n", "\\n") + "\"";
    }

    private static void reply(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
    }
}
