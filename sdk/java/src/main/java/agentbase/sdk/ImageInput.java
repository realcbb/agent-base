package agentbase.sdk;

import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.Set;

/**
 * 一条图片输入：url 与 data_base64 二选一（契约 images 字段）。
 * mediaType 支持 image/png、image/jpeg、image/webp、image/gif。
 */
public final class ImageInput {
    private static final Set<String> MEDIA_TYPES =
            Set.of("image/png", "image/jpeg", "image/webp", "image/gif");

    private final String url;
    private final String dataBase64;
    private final String mediaType;

    private ImageInput(String url, String dataBase64, String mediaType) {
        if (mediaType == null || !MEDIA_TYPES.contains(mediaType)) {
            throw new IllegalArgumentException("不支持的图片类型：" + mediaType);
        }
        this.url = url;
        this.dataBase64 = dataBase64;
        this.mediaType = mediaType;
    }

    /** 以可访问的图片 URL 引用（业务侧文件服务地址）。 */
    public static ImageInput url(String url, String mediaType) {
        if (url == null || url.isBlank()) {
            throw new IllegalArgumentException("url 不能为空");
        }
        return new ImageInput(url, null, mediaType);
    }

    /** 以 base64 内容直传（单张不超过约 10MB 二进制）。 */
    public static ImageInput base64(String dataBase64, String mediaType) {
        if (dataBase64 == null || dataBase64.isBlank()) {
            throw new IllegalArgumentException("dataBase64 不能为空");
        }
        return new ImageInput(null, dataBase64, mediaType);
    }

    ObjectNode toJson() {
        ObjectNode node = JsonNodeFactory.instance.objectNode();
        if (url != null) {
            node.put("url", url);
        } else {
            node.put("data_base64", dataBase64);
        }
        node.put("media_type", mediaType);
        return node;
    }
}
