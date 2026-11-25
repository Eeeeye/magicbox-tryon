package tryon;

import java.time.Instant;

public final class Task {
    private final String id;
    private final Instant createdAt;
    private final ImageMeta person;
    private final ImageMeta garment;
    private final String mode;
    private final String category;
    private final String idempotencyKey;
    private volatile Snapshot snapshot;

    public Task(String id, ImageMeta person, ImageMeta garment, String mode, String category, String key) {
        this(id, person, garment, mode, category, key, Instant.now(), null);
    }
    Task(String id, ImageMeta person, ImageMeta garment, String mode, String category, String key, Instant createdAt, Snapshot restored) {
        this.id = id;
        this.person = person;
        this.garment = garment;
        this.mode = mode;
        this.category = category;
        this.idempotencyKey = key;
        this.createdAt = createdAt;
        this.snapshot = restored == null
            ? new Snapshot(TaskStatus.QUEUED, "", null, null, createdAt, createdAt, null, null) : restored;
    }
    String idempotencyKey() { return idempotencyKey; }
    public String category() { return category; }
    public String id() { return id; }
    public Instant createdAt() { return createdAt; }
    public Instant updatedAt() { return snapshot.updatedAt(); }
    public TaskStatus status() { return snapshot.status(); }
    public String error() { return snapshot.error(); }
    public ImageMeta person() { return person; }
    public ImageMeta garment() { return garment; }
    public String result() { return snapshot.result(); }
    public Long durationMs() { return snapshot.durationMs(); }
    public String mode() { return mode; }
    /** Read all mutable task state from one volatile publication. */
    public Snapshot snapshot() { return snapshot; }
    void publish(Snapshot next) { snapshot = next; }
    Snapshot next(TaskStatus status, String error, String result, Long elapsedMs) {
        Snapshot previous = snapshot;
        Instant now = Instant.now();
        Instant runningAt = previous.runningAt();
        if (status == TaskStatus.RUNNING && runningAt == null) runningAt = now;
        Instant completedAt = previous.completedAt();
        if ((status == TaskStatus.SUCCEEDED || status == TaskStatus.FAILED) && completedAt == null) completedAt = now;
        return new Snapshot(status, error, result, elapsedMs, now,
            previous.queuedAt(), runningAt, completedAt);
    }

    public record Snapshot(
        TaskStatus status,
        String error,
        String result,
        Long durationMs,
        Instant updatedAt,
        Instant queuedAt,
        Instant runningAt,
        Instant completedAt
    ) {}
}
