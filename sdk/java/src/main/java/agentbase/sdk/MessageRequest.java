package agentbase.sdk;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.ArrayList;
import java.util.List;

/**
 * 一条用户消息：文本可为空但文本与图片不能同时为空；
 * 单条消息最多 8 张图片（契约上限，网关侧亦有校验）。
 */
public final class MessageRequest {
    private final String text;
    private final List<ImageInput> images;

    private MessageRequest(String text, List<ImageInput> images) {
        this.text = text == null ? "" : text;
        this.images = List.copyOf(images);
        if (this.text.isBlank() && this.images.isEmpty()) {
            throw new IllegalArgumentException("text 与 images 至少要有一个非空");
        }
        if (this.images.size() > 8) {
            throw new IllegalArgumentException("单条消息图片数超过上限 8");
        }
    }

    public static MessageRequest text(String text) {
        return new MessageRequest(text, List.of());
    }

    public static Builder builder() {
        return new Builder();
    }

    public static final class Builder {
        private String text = "";
        private final List<ImageInput> images = new ArrayList<>();

        public Builder text(String text) {
            this.text = text;
            return this;
        }

        public Builder addImage(ImageInput image) {
            this.images.add(image);
            return this;
        }

        public MessageRequest build() {
            return new MessageRequest(text, images);
        }
    }

    public String text() {
        return text;
    }

    public List<ImageInput> images() {
        return images;
    }

    ObjectNode toJson() {
        ObjectNode node = JsonNodeFactory.instance.objectNode();
        node.put("text", text);
        if (!images.isEmpty()) {
            ArrayNode arr = node.putArray("images");
            for (ImageInput image : images) {
                arr.add(image.toJson());
            }
        }
        return node;
    }
}
