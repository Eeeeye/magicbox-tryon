package tryon;

import java.nio.file.Path;
import java.time.Duration;

public record Config(int port, int httpThreads, Path dataDirectory, String inferenceUrl,
    String legacyInferenceUrl, Duration inferenceTimeout, int maxImageBytes, int maxBodyBytes,
    int maxPixels, int queueCapacity, int maxTaskHistory, Duration taskRetention, boolean realInference) {
    public static Config fromEnvironment() {
        int imageLimit = integer("MAX_IMAGE_BYTES", 5 * 1024 * 1024);
        String mode = System.getenv().getOrDefault("TRYON_MODE", "mock");
        if (!mode.equals("mock") && !mode.equals("catvton")) throw new IllegalArgumentException("Unknown TRYON_MODE");
        boolean real = mode.equals("catvton");
        return new Config(integer("PORT", 8080), integer("HTTP_THREADS", 8),
            Path.of(System.getenv().getOrDefault("DATA_DIR", "data/tryon")).toAbsolutePath().normalize(),
            System.getenv().getOrDefault("TRYON_INFERENCE_URL", "http://127.0.0.1:8091/infer/images"),
            System.getenv().getOrDefault("INFERENCE_URL", "http://127.0.0.1:8091/infer"),
            Duration.ofMillis(integer("INFERENCE_TIMEOUT_MS", real ? 600_000 : 3000)), imageLimit,
            integer("MAX_BODY_BYTES", imageLimit * 2 + 512 * 1024), integer("MAX_IMAGE_PIXELS", 16_000_000),
            integer("QUEUE_CAPACITY", 4), integer("MAX_TASK_HISTORY", 1000),
            Duration.ofSeconds(integer("TASK_RETENTION_SECONDS", 7 * 24 * 60 * 60)), real);
    }
    public String mode() { return realInference ? "catvton" : "mock"; }
    private static int integer(String name, int fallback) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) return fallback;
        try { int parsed = Integer.parseInt(value); if (parsed <= 0) throw new IllegalArgumentException(name + " must be positive"); return parsed; }
        catch (NumberFormatException e) { throw new IllegalArgumentException(name + " must be an integer", e); }
    }
}
