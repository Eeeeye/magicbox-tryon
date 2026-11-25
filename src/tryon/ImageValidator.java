package tryon;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.security.MessageDigest;
import java.util.HexFormat;
import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;

public final class ImageValidator {
    private ImageValidator() {}
    static void validateResult(byte[] bytes) throws IOException {
        Header h = header(bytes);
        if (h == null || !h.mime.equals("image/png") || h.width > 1024 || h.height > 1024)
            throw new InferenceClient.InferenceFailure("INVALID_RESULT_IMAGE");
        try {
            BufferedImage image = ImageIO.read(new ByteArrayInputStream(bytes));
            if (image == null || image.getWidth() != h.width || image.getHeight() != h.height)
                throw new IOException("Cannot decode result");
        } catch (IOException e) {
            throw new InferenceClient.InferenceFailure("INVALID_RESULT_IMAGE");
        }
    }
    public static ImageMeta validate(String role, MultipartPart part, Config config, String path) throws IOException {
        byte[] bytes = part.bytes();
        if (bytes.length == 0) throw new ValidationException("EMPTY_IMAGE");
        if (bytes.length > config.maxImageBytes()) throw new PayloadTooLargeException("IMAGE_TOO_LARGE");
        Header h = header(bytes);
        if (h == null) throw new ValidationException("UNSUPPORTED_OR_INVALID_IMAGE");
        if ((long) h.width * h.height > config.maxPixels()) throw new ValidationException("IMAGE_PIXEL_LIMIT_EXCEEDED");
        BufferedImage image;
        try {
            image = ImageIO.read(new ByteArrayInputStream(bytes));
        } catch (IOException e) {
            throw new ValidationException("IMAGE_DECODE_FAILED");
        }
        if (image == null || image.getWidth() != h.width || image.getHeight() != h.height)
            throw new ValidationException("IMAGE_DECODE_FAILED");
        String requested = part.contentType().toLowerCase();
        if (!requested.isBlank() && !requested.equals(h.mime)) throw new ValidationException("IMAGE_CONTENT_TYPE_MISMATCH");
        return new ImageMeta(role, h.mime, bytes.length, h.width, h.height, sha256(bytes), path);
    }
    public static String sha256(byte[] bytes) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
        catch (java.security.NoSuchAlgorithmException e) { throw new AssertionError(e); }
    }
    private static Header header(byte[] b) {
        if (b.length >= 24 && (b[0]&255)==137 && b[1]==80 && b[2]==78 && b[3]==71 && b[4]==13 && b[5]==10 && b[6]==26 && b[7]==10
            && b[12]=='I' && b[13]=='H' && b[14]=='D' && b[15]=='R') {
            int w=int32(b,16), h=int32(b,20); if (w>0 && h>0) return new Header("image/png",w,h);
        }
        if (b.length >= 4 && (b[0]&255)==0xff && (b[1]&255)==0xd8) {
            int i=2;
            while (i+9 < b.length) {
                if ((b[i]&255)!=0xff) { i++; continue; }
                int marker=b[i+1]&255; i+=2;
                if (marker==0xd8 || marker==0xd9 || (marker>=0xd0 && marker<=0xd7)) continue;
                if (i+1>=b.length) return null;
                int segment=((b[i]&255)<<8)|(b[i+1]&255); if (segment<2 || i+segment>b.length) return null;
                boolean sof=(marker>=0xc0&&marker<=0xc3)||(marker>=0xc5&&marker<=0xc7)||(marker>=0xc9&&marker<=0xcb)||(marker>=0xcd&&marker<=0xcf);
                if (sof && segment>=7) { int h=((b[i+3]&255)<<8)|(b[i+4]&255), w=((b[i+5]&255)<<8)|(b[i+6]&255); return w>0&&h>0?new Header("image/jpeg",w,h):null; }
                i+=segment;
            }
        }
        return null;
    }
    private static int int32(byte[] b,int i) { return ((b[i]&255)<<24)|((b[i+1]&255)<<16)|((b[i+2]&255)<<8)|(b[i+3]&255); }
    private record Header(String mime,int width,int height) {}
    public static class ValidationException extends IOException { public ValidationException(String m) { super(m); } }
    public static class PayloadTooLargeException extends IOException { public PayloadTooLargeException(String m) { super(m); } }
}
