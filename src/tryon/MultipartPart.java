package tryon;
public record MultipartPart(String name, String filename, String contentType, byte[] bytes) {}
