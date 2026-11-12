from __future__ import annotations

import unittest

from tgcurator.application.routing import RestrictedTemplateRenderer, build_rendering_context
from tgcurator.shared import DomainValidationError


class RestrictedTemplateRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = RestrictedTemplateRenderer()
        self.context = build_rendering_context(
            {
                "message": {
                    "id": "message-1",
                    "original_text": "hello",
                    "primary_telegram_message_id": 42,
                    "media_count": 1,
                },
                "source_channel": {"username": "source", "display_name": "Source"},
                "labels": {"effective": {"global": {"keep": {"activated": True}}}},
                "duplicate": {"status": "unique"},
                "media": [{"kind": "image", "id": "image-1"}],
                "blocked_from_analysis": False,
                "review_status": "approved",
            }
        )

    def test_render_is_strict_deterministic_and_snapshotted(self) -> None:
        first = self.renderer.render(
            template_text=(
                "{{ source_channel.display_name }}: {{ message.original_text }} "
                "{{ telegram_source_link }} {{ media_count }}"
            ),
            context=self.context,
        )
        second = self.renderer.render(
            template_text=(
                "{{ source_channel.display_name }}: {{ message.original_text }} "
                "{{ telegram_source_link }} {{ media_count }}"
            ),
            context=dict(reversed(tuple(self.context.items()))),
        )

        self.assertEqual(first.text, "Source: hello https://t.me/source/42 1")
        self.assertEqual(first.context, second.context)
        self.assertEqual(first.snapshot_hash, second.snapshot_hash)
        self.assertEqual(len(first.snapshot_hash), 64)

    def test_undefined_and_unsafe_syntax_are_rejected(self) -> None:
        invalid_templates = (
            "{{ message.missing }}",
            "{{ message.original_text | upper }}",
            "{{ message['original_text'] }}",
            "{{ message.original_text.upper() }}",
            "{% for item in media %}{{ item }}{% endfor %}",
            "{# hidden #}",
        )
        for template in invalid_templates:
            with self.subTest(template=template):
                with self.assertRaises(DomainValidationError):
                    self.renderer.render(template_text=template, context=self.context)

    def test_context_requires_explicit_message_source_and_media_facts(self) -> None:
        for facts in (
            {"source_channel": {}, "media": []},
            {"message": {}, "media": []},
            {"message": {}, "source_channel": {}, "media": {}},
        ):
            with self.subTest(facts=facts):
                with self.assertRaises(DomainValidationError):
                    build_rendering_context(facts)


if __name__ == "__main__":
    unittest.main()
