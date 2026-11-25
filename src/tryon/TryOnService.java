package tryon;

import java.io.IOException;
import java.util.List;
import java.util.Optional;
import java.util.Map;
import java.util.EnumMap;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

public final class TryOnService implements AutoCloseable {
    private static final System.Logger LOG = System.getLogger(TryOnService.class.getName());
    private final Config config;
    private final TaskRepository repository;
    private final ImageStorage storage;
    private final InferenceClient inference;
    private final ThreadPoolExecutor worker;
    private final ScheduledExecutorService maintenance = Executors.newSingleThreadScheduledExecutor(
        Thread.ofPlatform().daemon().name("tryon-retention").factory());
    private final ConcurrentHashMap<String, DemoTask> demoTasks = new ConcurrentHashMap<>();
    public TryOnService(Config config, TaskRepository repository, ImageStorage storage, InferenceClient inference) {
        this.config = config;
        this.repository = repository;
        this.storage = storage;
        this.inference = inference;
        this.worker = new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(config.queueCapacity()));
        long interval = Math.min(60, config.taskRetention().toSeconds());
        maintenance.scheduleWithFixedDelay(repository::sweep, interval, interval, TimeUnit.SECONDS);
    }

    public Task submit(List<MultipartPart> parts, String category, String key) throws IOException {
        if (key != null && !key.matches("[A-Za-z0-9._-]{1,96}"))
            throw new MultipartParser.BadRequestException("INVALID_IDEMPOTENCY_KEY");
        if (!List.of("upper", "lower", "dress").contains(category))
            throw new MultipartParser.BadRequestException("INVALID_CATEGORY");
        Map<String, MultipartPart> imagesByRole = new java.util.HashMap<>();
        for (MultipartPart part : parts) {
            if (!part.name().equals("person") && !part.name().equals("garment"))
                throw new MultipartParser.BadRequestException("UNEXPECTED_FIELD");
            if (imagesByRole.putIfAbsent(part.name(), part) != null)
                throw new MultipartParser.BadRequestException("DUPLICATE_IMAGE_FIELD");
        }
        if (imagesByRole.size() != 2) throw new MultipartParser.BadRequestException("TWO_IMAGES_REQUIRED");
        ImageStorage.StagedImages images = storage.stage(imagesByRole.get("person"), imagesByRole.get("garment"), config);
        try {
            Task task = admit(images, category, key);
            if (!task.id().equals(images.id())) storage.delete(images.id());
            return task;
        } catch (IOException | RuntimeException e) {
            storage.delete(images.id());
            throw e;
        }
    }

    // Serialize admission only. Validation, disk staging and inference run outside this lock.
    private synchronized Task admit(ImageStorage.StagedImages images, String category, String key) throws IOException {
        var existing = repository.byKey(key);
        if (existing.isPresent()) {
            Task task = existing.get();
            if (!task.category().equals(category) || !task.mode().equals(config.mode())
                || !task.person().sha256().equals(images.person().sha256())
                || !task.garment().sha256().equals(images.garment().sha256()))
                throw new ConflictException("IDEMPOTENCY_KEY_REUSED");
            return task;
        }
        if (worker.getQueue().remainingCapacity() == 0) throw new BusyException("QUEUE_FULL");
        Task task = new Task(images.id(), images.person(), images.garment(), config.mode(), category, key);
        if (!repository.saveIfCapacity(task, config.maxTaskHistory())) throw new BusyException("TASK_HISTORY_FULL");
        try {
            worker.execute(() -> process(task, images));
        } catch (RejectedExecutionException e) {
            repository.retire(task);
            throw new BusyException("QUEUE_FULL");
        }
        return task;
    }
    private void process(Task task,ImageStorage.StagedImages images) {
        long started = System.nanoTime();
        String error;
        try {
            repository.update(task, TaskStatus.RUNNING, "", null, null);
            byte[] body = inference.infer(images, task.category());
            String result = InferenceClient.MOCK_RESULT;
            if (config.realInference()) {
                storage.saveResult(images, body);
                result = TaskRepository.resultUrl(task);
            }
            long elapsed = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started);
            repository.update(task, TaskStatus.SUCCEEDED, "", result, elapsed);
            LOG.log(System.Logger.Level.INFO, "Completed {0}, elapsedMs={1}", task.id(), elapsed);
            return;
        } catch (InferenceClient.InferenceFailure e) {
            error = e.getMessage();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            error = "INTERRUPTED";
        } catch (IOException | RuntimeException e) {
            LOG.log(System.Logger.Level.ERROR, "Processing failed for " + task.id(), e);
            error = "TASK_PROCESSING_ERROR";
        }
        try {
            repository.update(task, TaskStatus.FAILED, error, null, TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started));
        } catch (IOException e) {
            LOG.log(System.Logger.Level.ERROR, "Cannot persist failure for " + task.id(), e);
            task.publish(task.next(TaskStatus.FAILED, "STORAGE_ERROR", null, null));
        }
    }

    public DemoTask submitDemo() throws IOException {
        DemoTask task;
        synchronized (demoTasks) {
            if (demoTasks.size() >= config.maxTaskHistory()) throw new BusyException("DEMO_HISTORY_FULL_RESTART_REQUIRED");
            task=new DemoTask(UUID.randomUUID().toString()); demoTasks.put(task.id(),task);
        }
        try { worker.execute(()->{ task.running(); String error=inference.inferLegacy(config.legacyInferenceUrl()); if(error.isEmpty()) task.succeeded("MOCK_ONLY_NO_IMAGE_GENERATED"); else task.failed(error); }); }
        catch (RejectedExecutionException e) { demoTasks.remove(task.id()); throw new BusyException("QUEUE_FULL"); }
        return task;
    }
    public Optional<Task> find(String id) { return repository.find(id); }
    public List<Task> latest(int limit) { return repository.latest(limit); }
    public int queueDepth() { return worker.getQueue().size(); }
    public int activeWorkers() { return worker.getActiveCount(); }
    public boolean isReady() { return !worker.isShutdown() && inference.ready(); }
    public Map<TaskStatus, Long> statusCounts() {
        Map<TaskStatus, Long> counts = new EnumMap<>(TaskStatus.class);
        for (TaskStatus status : TaskStatus.values()) counts.put(status, repository.count(status));
        return counts;
    }
    public Optional<DemoTask> findDemo(String id) { return Optional.ofNullable(demoTasks.get(id)); }
    public TaskRepository repository() { return repository; }
    public void close() {
        maintenance.shutdownNow();
        worker.shutdownNow();
        try {
            if (!maintenance.awaitTermination(5, TimeUnit.SECONDS))
                LOG.log(System.Logger.Level.WARNING, "Task cleanup still stopping");
            if (!worker.awaitTermination(5, TimeUnit.SECONDS))
                LOG.log(System.Logger.Level.WARNING, "Inference worker still stopping");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
    public static class ConflictException extends IOException { ConflictException(String message) { super(message); } }
    public static class BusyException extends IOException { public BusyException(String m){super(m);} }

    public static final class DemoTask {
        private final String id; private volatile TaskStatus status=TaskStatus.QUEUED; private volatile String error=""; private volatile String result;
        DemoTask(String id){this.id=id;} public String id(){return id;} public TaskStatus status(){return status;} public String error(){return error;} public String result(){return result;}
        void running(){status=TaskStatus.RUNNING;} void succeeded(String r){status=TaskStatus.SUCCEEDED;result=r;} void failed(String e){status=TaskStatus.FAILED;error=e;}
    }
}
