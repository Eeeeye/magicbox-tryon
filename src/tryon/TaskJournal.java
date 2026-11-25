package tryon;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.Properties;

/** A task's result is committed first, followed by its atomic status snapshot. */
final class TaskJournal {
    static void store(Path directory, Task task, Task.Snapshot state) throws IOException {
        Properties data = new Properties();
        data.setProperty("version", "1");
        data.setProperty("mode", task.mode());
        if (task.idempotencyKey() != null) data.setProperty("idempotencyKey", task.idempotencyKey());
        data.setProperty("category", task.category());
        data.setProperty("createdAt", task.createdAt().toString());
        data.setProperty("status", state.status().name());
        data.setProperty("updatedAt", state.updatedAt().toString());
        data.setProperty("error", state.error());
        if (state.result() != null) data.setProperty("result", state.result());
        if (state.durationMs() != null) data.setProperty("durationMs", state.durationMs().toString());
        if (state.runningAt() != null) data.setProperty("runningAt", state.runningAt().toString());
        if (state.completedAt() != null) data.setProperty("completedAt", state.completedAt().toString());
        writeMeta(data, task.person());
        writeMeta(data, task.garment());
        Path staging = Files.createTempFile(directory, ".task-", ".tmp");
        try {
            try (OutputStream out = Files.newOutputStream(staging)) { data.store(out, null); }
            commit(staging, directory.resolve("task.properties"));
        } finally { Files.deleteIfExists(staging); }
    }

    static void commit(Path staging, Path target) throws IOException {
        try (FileChannel channel = FileChannel.open(staging, StandardOpenOption.WRITE)) { channel.force(true); }
        Files.move(staging, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }

    static Task read(Path directory) throws IOException {
        Properties data = new Properties();
        try (var in = Files.newInputStream(directory.resolve("task.properties"))) { data.load(in); }
        if (!"1".equals(data.getProperty("version"))) throw new IOException("Unsupported task journal version");
        Instant created = Instant.parse(data.getProperty("createdAt"));
        String duration = data.getProperty("durationMs");
        Task.Snapshot state = new Task.Snapshot(TaskStatus.valueOf(data.getProperty("status")),
            data.getProperty("error", ""), data.getProperty("result"), duration == null ? null : Long.valueOf(duration),
            Instant.parse(data.getProperty("updatedAt")), created, instant(data, "runningAt"), instant(data, "completedAt"));
        return new Task(directory.getFileName().toString(), readMeta(directory, data, "person"),
            readMeta(directory, data, "garment"), data.getProperty("mode"), data.getProperty("category", "upper"),
            data.getProperty("idempotencyKey"), created, state);
    }

    private static Instant instant(Properties data, String name) {
        String value = data.getProperty(name);
        return value == null ? null : Instant.parse(value);
    }

    private static void writeMeta(Properties data, ImageMeta image) {
        String prefix = image.role() + ".";
        data.setProperty(prefix + "mime", image.mimeType());
        data.setProperty(prefix + "bytes", Long.toString(image.bytes()));
        data.setProperty(prefix + "width", Integer.toString(image.width()));
        data.setProperty(prefix + "height", Integer.toString(image.height()));
        data.setProperty(prefix + "sha256", image.sha256());
    }

    private static ImageMeta readMeta(Path directory, Properties data, String role) {
        String prefix = role + ".";
        return new ImageMeta(role, data.getProperty(prefix + "mime"), Long.parseLong(data.getProperty(prefix + "bytes")),
            Integer.parseInt(data.getProperty(prefix + "width")), Integer.parseInt(data.getProperty(prefix + "height")),
            data.getProperty(prefix + "sha256"), directory.resolve(role + ".bin").toString());
    }
}
