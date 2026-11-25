package tryon;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.UUID;

public final class ImageStorage {
    private static final System.Logger LOG = System.getLogger(ImageStorage.class.getName());
    private final Path root;

    public ImageStorage(Path root) { this.root = root; }

    public void saveResult(StagedImages images, byte[] bytes) throws IOException {
        ImageValidator.validateResult(bytes);
        Path temporary = Files.createTempFile(images.directory(), ".result-", ".png");
        try {
            Files.write(temporary, bytes);
            TaskJournal.commit(temporary, images.directory().resolve("result.png"));
        } finally {
            Files.deleteIfExists(temporary);
        }
    }

    public StagedImages stage(MultipartPart personPart, MultipartPart garmentPart, Config config) throws IOException {
        String id = UUID.randomUUID().toString();
        Path directory = root.resolve(id);
        Path personPath = directory.resolve("person.bin");
        Path garmentPath = directory.resolve("garment.bin");
        ImageMeta person = ImageValidator.validate("person", personPart, config, personPath.toString());
        ImageMeta garment = ImageValidator.validate("garment", garmentPart, config, garmentPath.toString());
        Files.createDirectory(directory);
        try {
            Files.write(personPath, personPart.bytes());
            Files.write(garmentPath, garmentPart.bytes());
            return new StagedImages(id, directory, person, garment);
        } catch (IOException e) {
            delete(id);
            throw e;
        }
    }

    void delete(String id) {
        Path directory = root.resolve(UUID.fromString(id).toString());
        try {
            deleteDirectory(directory);
        } catch (java.nio.file.NoSuchFileException ignored) {
            // Queue rejection can already have moved this directory out of live storage.
        } catch (IOException e) {
            LOG.log(System.Logger.Level.ERROR, "Cannot clean up staging directory for task " + id, e);
        }
    }

    static void deleteDirectory(Path directory) throws IOException {
        try (var paths = Files.walk(directory)) {
            for (Path path : paths.sorted(Comparator.reverseOrder()).toList()) Files.deleteIfExists(path);
        }
    }

    public record StagedImages(String id, Path directory, ImageMeta person, ImageMeta garment) {}
}
