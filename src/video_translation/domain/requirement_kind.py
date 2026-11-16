"""Map the platform's delivery field to a supported production owner."""

TRANSLATION_MODES = {
    "video_translate": ("video_translation", "video"),
    "image_translate_or_resize": ("image_translation", "image"),
}


def translation_kind(brief):
    mode, asset_type = brief.get("aiMode"), brief.get("assetType")
    selected = TRANSLATION_MODES.get(mode)
    if selected:
        if asset_type != selected[1]:
            raise ValueError("AI承接交付方式与需求素材类型不一致")
        return selected[0]
    # Existing explicit video intakes predate the optional delivery-mode field.
    if mode is None and asset_type == "video":
        return "video_translation"
    raise ValueError("此需求尚不支持导入，请选择AI承接交付为图片翻译或视频翻译的需求")
