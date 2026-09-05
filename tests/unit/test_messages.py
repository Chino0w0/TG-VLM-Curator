import unittest

from tgcurator.domain.messages import (
    MediaAsset,
    MediaKind,
    MessageContent,
    message_visual_fingerprint,
)


class MessageVisualFingerprintTests(unittest.TestCase):
    def test_is_independent_of_text_and_media_order(self) -> None:
        image = MediaAsset("image-1", MediaKind.IMAGE, original_visual_phash="A1" * 8)
        video = MediaAsset("video-1", MediaKind.VIDEO, video_cover_phash="B2" * 8)
        first = MessageContent(text="first caption", media=(image, video))
        second = MessageContent(text="edited caption", media=(video, image))
        self.assertEqual(first.visual_fingerprint, second.visual_fingerprint)

    def test_uses_a_multiset_not_a_set(self) -> None:
        image_a = MediaAsset("image-a", MediaKind.IMAGE, original_visual_phash="A1" * 8)
        image_b = MediaAsset("image-b", MediaKind.IMAGE, original_visual_phash="A1" * 8)
        self.assertNotEqual(
            message_visual_fingerprint((image_a,)),
            message_visual_fingerprint((image_a, image_b)),
        )

    def test_text_only_message_has_no_visual_identity(self) -> None:
        self.assertIsNone(MessageContent(text="no media").visual_fingerprint)

    def test_representative_video_frames_do_not_change_identity(self) -> None:
        first = MediaAsset(
            "video-1",
            MediaKind.VIDEO,
            video_cover_phash="C3" * 8,
            representative_frame_phashes=("D4" * 8,),
        )
        second = MediaAsset(
            "video-2",
            MediaKind.VIDEO,
            video_cover_phash="C3" * 8,
            representative_frame_phashes=("E5" * 8,),
        )
        self.assertEqual(
            message_visual_fingerprint((first,)), message_visual_fingerprint((second,))
        )


if __name__ == "__main__":
    unittest.main()
