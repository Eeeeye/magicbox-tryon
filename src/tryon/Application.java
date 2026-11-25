package tryon;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.StandardOpenOption;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

public final class Application {
    private Application() {}
    public static void main(String[] args) throws Exception {
        Config config = Config.fromEnvironment();
        Files.createDirectories(config.dataDirectory());
        FileChannel lockChannel = FileChannel.open(config.dataDirectory().resolve(".instance.lock"),
            StandardOpenOption.CREATE, StandardOpenOption.WRITE);
        var lock = lockChannel.tryLock();
        if (lock == null) throw new IllegalStateException("Another process owns DATA_DIR");
        TaskRepository repository = new TaskRepository(config.dataDirectory(), config.taskRetention());
        ImageStorage storage = new ImageStorage(config.dataDirectory());
        InferenceClient inference = new InferenceClient(config);
        TryOnService service = new TryOnService(config, repository, storage, inference);
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", config.port()), 0);
        // If the HTTP pool fills, let the accepting dispatcher apply backpressure.
        var workers = new ThreadPoolExecutor(config.httpThreads(), config.httpThreads(), 0L, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(4 * config.httpThreads()), new ThreadPoolExecutor.CallerRunsPolicy());
        server.setExecutor(workers);
        server.createContext("/", new TryOnHttpHandler(config, service));
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            server.stop(0); service.close(); workers.shutdownNow();
            try { lockChannel.close(); } catch (java.io.IOException e) { System.err.println(e.getMessage()); }
        }));
        server.start();
        System.out.println("Try-on service: http://127.0.0.1:" + config.port()
            + " (" + config.mode() + ")");
    }
}
