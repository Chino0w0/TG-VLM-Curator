from __future__ import annotations

from tgcurator.application.ports.admin_api import MetricsCollectionSnapshot


def render_prometheus_metrics(snapshot: MetricsCollectionSnapshot) -> str:
    """Render Prometheus 0.0.4 text exposition covering all architectural telemetry families."""

    lines: list[str] = []

    def header(name: str, mtype: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")

    header(
        "tgcurator_messages_total",
        "gauge",
        "Logical messages grouped by current processing status.",
    )
    for status in ("ingested", "processing", "processed", "failed"):
        count = snapshot.messages_by_status.get(status, 0)
        lines.append(f'tgcurator_messages_total{{status="{_escape(status)}"}} {count}')

    header(
        "tgcurator_messages_blocked_total",
        "gauge",
        "Messages short-circuited by an activated Negative Gate label.",
    )
    lines.append(f"tgcurator_messages_blocked_total {snapshot.messages_blocked_total}")

    header(
        "tgcurator_processing_ranges_total",
        "gauge",
        "Processing ranges grouped by lifecycle status.",
    )
    for status in ("pending", "active", "paused", "completed", "failed"):
        count = snapshot.ranges_by_status.get(status, 0)
        lines.append(f'tgcurator_processing_ranges_total{{status="{_escape(status)}"}} {count}')

    header(
        "tgcurator_processing_range_lag_messages",
        "gauge",
        "Message ID distance between active/latest watermark ceiling and continuous watermark.",
    )
    for range_id, lag in sorted(snapshot.range_lags.items()):
        lines.append(
            f'tgcurator_processing_range_lag_messages{{range_id="{_escape(range_id)}"}} {lag}'
        )

    header(
        "tgcurator_media_assets_total",
        "gauge",
        "Media assets grouped by asset kind and archive state.",
    )
    for (kind, state), count in sorted(snapshot.media_assets_by_kind_state.items()):
        lines.append(
            f'tgcurator_media_assets_total{{kind="{_escape(kind)}",state="{_escape(state)}"}} '
            f"{count}"
        )

    header(
        "tgcurator_archive_bytes_total",
        "gauge",
        "Total ready normalized WebP image and video frame bytes stored in archive.",
    )
    lines.append(f"tgcurator_archive_bytes_total {snapshot.archive_bytes_total}")

    header(
        "tgcurator_analysis_stage_runs_total",
        "gauge",
        "Analysis stage runs grouped by status and result origin.",
    )
    for (status, origin), count in sorted(snapshot.stage_runs_by_status_origin.items()):
        lines.append(
            f'tgcurator_analysis_stage_runs_total{{status="{_escape(status)}",'
            f'result_origin="{_escape(origin)}"}} {count}'
        )

    header(
        "tgcurator_analysis_cache_hits_total",
        "gauge",
        "Stage runs satisfied via semantic stage cache reuse.",
    )
    lines.append(f"tgcurator_analysis_cache_hits_total {snapshot.analysis_cache_hits_total}")

    header(
        "tgcurator_inference_calls_total",
        "gauge",
        "External inference call audit records grouped by status and error code.",
    )
    for (status, err), count in sorted(snapshot.inference_calls_by_status_error.items()):
        lines.append(
            f'tgcurator_inference_calls_total{{status="{_escape(status)}",'
            f'error_code="{_escape(err)}"}} {count}'
        )

    header(
        "tgcurator_inference_latency_ms_sum",
        "gauge",
        "Cumulative external inference latency in milliseconds.",
    )
    lines.append(f"tgcurator_inference_latency_ms_sum {snapshot.inference_latency_ms_sum:.3f}")

    header(
        "tgcurator_inference_latency_ms_count",
        "gauge",
        "Count of external inference calls with recorded latency.",
    )
    lines.append(f"tgcurator_inference_latency_ms_count {snapshot.inference_latency_ms_count}")

    header(
        "tgcurator_inference_tokens_total",
        "gauge",
        "External inference token usage grouped by token counter kind.",
    )
    for kind, count in sorted(snapshot.inference_tokens_by_kind.items()):
        lines.append(f'tgcurator_inference_tokens_total{{kind="{_escape(kind)}"}} {count}')

    header(
        "tgcurator_routing_evaluations_total",
        "gauge",
        "Formal routing evaluations persisted in PostgreSQL.",
    )
    lines.append(f"tgcurator_routing_evaluations_total {snapshot.routing_evaluations_total}")

    header(
        "tgcurator_routing_matched_actions_total",
        "gauge",
        "Total publication intents generated from matched routing actions.",
    )
    lines.append(
        f"tgcurator_routing_matched_actions_total {snapshot.routing_matched_actions_total}"
    )

    pub = snapshot.operations.publication
    header(
        "tgcurator_publication_events_total",
        "counter",
        "In-process publication worker lifecycle counters.",
    )
    for event_name, value in (
        ("claimed", pub.claimed),
        ("sent", pub.sent),
        ("retry_scheduled", pub.retry_scheduled),
        ("failed", pub.failed),
        ("cancelled", pub.cancelled),
        ("reconciled_part", pub.reconciled_parts),
    ):
        lines.append(f'tgcurator_publication_events_total{{event="{event_name}"}} {value}')

    header(
        "tgcurator_publication_intents_total",
        "gauge",
        "Durable publication intents grouped by status.",
    )
    for status, count in sorted(snapshot.publication_intents_by_status.items()):
        lines.append(f'tgcurator_publication_intents_total{{status="{_escape(status)}"}} {count}')

    fw = snapshot.operations.flood_wait
    header(
        "tgcurator_telegram_flood_wait_events_total",
        "counter",
        "Observed Telegram FloodWait rate-limit responses.",
    )
    lines.append(f"tgcurator_telegram_flood_wait_events_total {fw.events}")

    header(
        "tgcurator_telegram_flood_wait_seconds_total",
        "counter",
        "Cumulative FloodWait delay seconds scheduled.",
    )
    lines.append(f"tgcurator_telegram_flood_wait_seconds_total {fw.wait_seconds_total:.3f}")

    header(
        "tgcurator_telegram_flood_wait_seconds_max",
        "gauge",
        "Maximum single FloodWait delay seconds observed.",
    )
    lines.append(f"tgcurator_telegram_flood_wait_seconds_max {fw.wait_seconds_max:.3f}")

    header(
        "tgcurator_worker_backlog_items",
        "gauge",
        "Pending or retry-due durable work items per queue.",
    )
    backlog = snapshot.operations.backlog
    default_queues = {
        "publication_pending": backlog.publication_pending,
        "publication_partial": backlog.publication_partial,
        "publication_retry_due": backlog.publication_retry_due,
        "cleanup_pending": backlog.cleanup_pending,
        "cleanup_retry_due": backlog.cleanup_retry_due,
        "durable_wakeups_due": backlog.durable_wakeups_due,
    }
    merged_queues = {**default_queues, **dict(snapshot.worker_backlog_by_queue)}
    for queue_name, count in sorted(merged_queues.items()):
        lines.append(f'tgcurator_worker_backlog_items{{queue="{_escape(queue_name)}"}} {count}')

    header(
        "tgcurator_service_ready",
        "gauge",
        "Service readiness state (1 = ready, 0 = not ready).",
    )
    lines.append(
        f'tgcurator_service_ready{{service="api"}} {1 if snapshot.api_database_ready else 0}'
    )
    lines.append(
        f'tgcurator_service_ready{{service="worker"}} '
        f"{1 if snapshot.operations.worker.ready else 0}"
    )
    lines.append(
        f'tgcurator_service_ready{{service="scheduler"}} '
        f"{1 if snapshot.operations.scheduler.ready else 0}"
    )

    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
