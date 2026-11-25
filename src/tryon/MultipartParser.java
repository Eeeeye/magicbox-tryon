package tryon;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Bounded parser for the service's two-file multipart contract. */
public final class MultipartParser {
    private MultipartParser() {}
    public static List<MultipartPart> parse(InputStream input, String contentType, int maxBody) throws IOException {
        String boundary = boundary(contentType);
        if (boundary == null || boundary.isBlank() || boundary.length() > 70)
            throw new BadRequestException("MULTIPART_BOUNDARY_REQUIRED");
        byte[] body = readLimited(input, maxBody);
        byte[] delimiter = ("--" + boundary).getBytes(StandardCharsets.ISO_8859_1);
        List<MultipartPart> parts = new ArrayList<>();
        int cursor = 0;
        while (true) {
            int marker = indexOf(body, delimiter, cursor);
            if (marker < 0) break;
            int after = marker + delimiter.length;
            if (after + 1 < body.length && body[after] == '-' && body[after + 1] == '-') break;
            if (!startsWith(body, after, "\r\n")) throw new BadRequestException("INVALID_MULTIPART_DELIMITER");
            if (parts.size() >= 2) throw new BadRequestException("TOO_MANY_IMAGE_FIELDS");
            int headerStart = after + 2;
            int headerEnd = indexOf(body, new byte[] {'\r','\n','\r','\n'}, headerStart);
            if (headerEnd < 0) throw new BadRequestException("MULTIPART_HEADERS_INCOMPLETE");
            if (headerEnd - headerStart > 8192) throw new BadRequestException("PART_HEADERS_TOO_LARGE");
            Map<String, String> headers = headers(body, headerStart, headerEnd);
            int dataStart = headerEnd + 4;
            int next = findPartBoundary(body, delimiter, dataStart);
            if (next < 0) throw new BadRequestException("MULTIPART_CLOSING_BOUNDARY_MISSING");
            String disposition = headers.get("content-disposition");
            String name = parameter(disposition, "name");
            String filename = parameter(disposition, "filename");
            if (name == null || filename == null || filename.isBlank())
                throw new BadRequestException("IMAGE_PART_REQUIRES_NAME_AND_FILENAME");
            parts.add(new MultipartPart(name, filename, headers.getOrDefault("content-type", ""),
                java.util.Arrays.copyOfRange(body, dataStart, next)));
            cursor = next + 2;
        }
        if (parts.isEmpty()) throw new BadRequestException("NO_MULTIPART_PARTS");
        return parts;
    }
    private static byte[] readLimited(InputStream in, int max) throws IOException {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buffer = new byte[8192]; int total = 0, count;
        while ((count = in.read(buffer)) != -1) {
            total += count; if (total > max) throw new PayloadTooLargeException("REQUEST_BODY_TOO_LARGE");
            out.write(buffer, 0, count);
        }
        return out.toByteArray();
    }
    private static Map<String, String> headers(byte[] body, int start, int end) {
        String text = new String(body, start, end - start, StandardCharsets.ISO_8859_1);
        Map<String, String> result = new HashMap<>();
        for (String line : text.split("\\r\\n")) {
            int colon = line.indexOf(':');
            if (colon > 0) result.put(line.substring(0, colon).trim().toLowerCase(), line.substring(colon + 1).trim());
        }
        return result;
    }
    private static String boundary(String contentType) {
        if (contentType == null || !contentType.toLowerCase().startsWith("multipart/form-data")) return null;
        for (String part : contentType.split(";")) {
            String p = part.trim();
            if (p.toLowerCase().startsWith("boundary=")) {
                String value = p.substring(9).trim();
                if (value.startsWith("\"") && value.endsWith("\"")) value = value.substring(1, value.length()-1);
                return value;
            }
        }
        return null;
    }
    private static String parameter(String header, String key) {
        if (header == null) return null;
        for (String item : header.split(";")) {
            String[] pair = item.trim().split("=", 2);
            if (pair.length == 2 && pair[0].trim().equalsIgnoreCase(key)) {
                String value = pair[1].trim();
                if (value.startsWith("\"") && value.endsWith("\"")) value = value.substring(1, value.length()-1);
                return value;
            }
        }
        return null;
    }
    private static boolean startsWith(byte[] data, int offset, String value) { return startsWith(data, offset, value.getBytes(StandardCharsets.ISO_8859_1)); }
    private static boolean startsWith(byte[] data, int offset, byte[] value) {
        if (offset + value.length > data.length) return false;
        for (int i=0; i<value.length; i++) if (data[offset+i] != value[i]) return false;
        return true;
    }
    private static int indexOf(byte[] data, byte[] needle, int from) {
        outer: for (int i=Math.max(0, from); i<=data.length-needle.length; i++) {
            for (int j=0; j<needle.length; j++) if (data[i+j] != needle[j]) continue outer;
            return i;
        }
        return -1;
    }
    /**
     * A boundary-looking byte sequence inside an image is payload unless the
     * boundary is followed by the legal multipart line ending or closing '--'.
     * Checking the suffix prevents a coincidental byte sequence from silently
     * truncating an uploaded image.
     */
    private static int findPartBoundary(byte[] body, byte[] delimiter, int from) {
        byte[] prefixed = concat(new byte[] {'\r', '\n'}, delimiter);
        int candidate = Math.max(0, from);
        while ((candidate = indexOf(body, prefixed, candidate)) >= 0) {
            int after = candidate + prefixed.length;
            if (startsWith(body, after, "\r\n") || startsWith(body, after, "--")) return candidate;
            candidate++;
        }
        return -1;
    }
    private static byte[] concat(byte[] a, byte[] b) {
        byte[] out = java.util.Arrays.copyOf(a, a.length+b.length); System.arraycopy(b,0,out,a.length,b.length); return out;
    }
    public static class BadRequestException extends IOException { public BadRequestException(String m) { super(m); } }
    public static class PayloadTooLargeException extends IOException { public PayloadTooLargeException(String m) { super(m); } }
}
