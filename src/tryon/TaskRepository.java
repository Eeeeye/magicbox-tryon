package tryon;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Duration;
import java.time.Instant;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Stream;

public final class TaskRepository {
    private static final System.Logger LOG = System.getLogger(TaskRepository.class.getName());
    private final ConcurrentHashMap<String, Task> tasks = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Task> keyed = new ConcurrentHashMap<>();
    private final Path root;
    private final Path expiredDirectory;
    private final Duration retention;

    public TaskRepository(Path root, Duration retention) throws IOException {
        this.root = root;
        this.retention = retention;
        expiredDirectory = root.resolve(".expired");
        Files.createDirectories(expiredDirectory);
        if (!Files.isDirectory(expiredDirectory, LinkOption.NOFOLLOW_LINKS))
            throw new IOException("Expected a local directory for retired tasks: " + expiredDirectory);
        try (var dirs = Files.newDirectoryStream(root)) {
            for (Path dir : dirs) {
                if (!isTaskDirectory(dir)) continue;
                Path journal = dir.resolve("task.properties");
                if (!Files.isRegularFile(journal, LinkOption.NOFOLLOW_LINKS)) {
                    if (Files.notExists(journal, LinkOption.NOFOLLOW_LINKS)) recoverUncommittedUpload(dir);
                    continue;
                }
                Task task = TaskJournal.read(dir);
                if (task.status() == TaskStatus.RUNNING || task.status() == TaskStatus.QUEUED) {
                    if (task.mode().equals("catvton") && Files.isRegularFile(dir.resolve("result.png"))) {
                        update(task, TaskStatus.SUCCEEDED, "", resultUrl(task), null);
                    } else {
                        update(task, TaskStatus.FAILED, "SERVER_RESTARTED", null, null);
                    }
                }
                index(task);
            }
        }
        sweep();
    }

    // Startup holds the instance lock. Only input files and a first journal temp file
    // can exist if the process stopped between staging and committing admission.
    private void recoverUncommittedUpload(Path dir) {
        try (var entries = Files.newDirectoryStream(dir)) {
            for (Path entry : entries) {
                String name = entry.getFileName().toString();
                if (!Files.isRegularFile(entry, LinkOption.NOFOLLOW_LINKS) ||
                    !(name.equals("person.bin") || name.equals("garment.bin") || name.matches("\\.task-[0-9]+\\.tmp"))) return;
            }
            Files.move(dir, expiredDirectory.resolve(dir.getFileName()), StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException | java.nio.file.DirectoryIteratorException e) {
            LOG.log(System.Logger.Level.WARNING, "Cannot reclaim incomplete upload " + dir.getFileName(), e);
        }
    }

    static String resultUrl(Task task) { return "/api/tryon/tasks/" + task.id() + "/result"; }

    public void update(Task task, TaskStatus status, String error, String result, Long duration) throws IOException {
        synchronized (task) {
            Task.Snapshot next = task.next(status, error, result, duration);
            TaskJournal.store(root.resolve(task.id()), task, next);
            task.publish(next);
        }
    }

    public synchronized boolean saveIfCapacity(Task task, int max) throws IOException {
        if (tasks.size() >= max) retireExpired();
        if (tasks.size() >= max) return false;
        TaskJournal.store(root.resolve(task.id()), task, task.snapshot());
        index(task);
        return true;
    }

    public Optional<Task> find(String id) {
        Task task = tasks.get(id);
        return task == null || expired(task, cutoff()) ? Optional.empty() : Optional.of(task);
    }

    synchronized Optional<Task> byKey(String key) throws IOException {
        Task task = key == null ? null : keyed.get(key);
        if (task != null && expired(task, cutoff())) {
            retire(task);
            return Optional.empty();
        }
        return Optional.ofNullable(task);
    }

    private Instant cutoff() { return Instant.now().minus(retention); }

    private static boolean expired(Task task, Instant cutoff) {
        Task.Snapshot state = task.snapshot();
        if (state.status() != TaskStatus.SUCCEEDED && state.status() != TaskStatus.FAILED) return false;
        Instant finished = state.completedAt() == null ? state.updatedAt() : state.completedAt();
        return !finished.isAfter(cutoff);
    }

    private void index(Task task) {
        tasks.put(task.id(), task);
        if (task.idempotencyKey() != null) keyed.put(task.idempotencyKey(), task);
    }

    // The rename is the on-disk commit. Restart can never resurrect a retired idempotency key.
    synchronized void retire(Task task) throws IOException {
        if (tasks.get(task.id()) != task) return;
        Files.move(root.resolve(task.id()), expiredDirectory.resolve(task.id()), StandardCopyOption.ATOMIC_MOVE);
        tasks.remove(task.id(), task);
        if (task.idempotencyKey() != null) keyed.remove(task.idempotencyKey(), task);
    }

    private synchronized void retireExpired() {
        Instant cutoff = cutoff();
        for (Task task : tasks.values()) {
            if (!expired(task, cutoff)) continue;
            try {
                retire(task);
            } catch (IOException e) {
                LOG.log(System.Logger.Level.ERROR, "Cannot retire expired task " + task.id(), e);
            }
        }
    }

    // Only startup and the single maintenance worker delete retired directories.
    void sweep() {
        retireExpired();
        try (var dirs = Files.newDirectoryStream(expiredDirectory)) {
            for (Path dir : dirs) {
                if (!isTaskDirectory(dir)) continue;
                try {
                    ImageStorage.deleteDirectory(dir);
                } catch (IOException e) {
                    LOG.log(System.Logger.Level.ERROR, "Cannot reclaim files for " + dir.getFileName(), e);
                }
            }
        } catch (IOException | java.nio.file.DirectoryIteratorException | java.io.UncheckedIOException e) {
            LOG.log(System.Logger.Level.ERROR, "Cannot scan retired task files", e);
        }
    }

    private static boolean isTaskDirectory(Path path) {
        if (!Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) return false;
        String name = path.getFileName().toString();
        try {
            return name.length() == 36 && UUID.fromString(name).toString().equals(name);
        } catch (IllegalArgumentException invalid) {
            return false;
        }
    }

    private Stream<Task> visibleTasks() {
        Instant cutoff = cutoff();
        return tasks.values().stream().filter(task -> !expired(task, cutoff));
    }

    public int size() { return Math.toIntExact(visibleTasks().count()); }

    public List<Task> latest(int limit) {
        return visibleTasks().sorted(Comparator.comparing(Task::createdAt).reversed())
            .limit(Math.max(0, limit)).toList();
    }

    public long count(TaskStatus status) {
        return visibleTasks().filter(task -> task.status() == status).count();
    }
}
