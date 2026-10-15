import json

from bbackup.progress import observe_progress, stdout_progress_handler


def test_fragmented_engine_progress_exposes_only_numeric_allowlist():
    events = []
    raw = json.dumps({"message_type": "status", "percent_done": 0.5,
                      "total_files": 10, "files_done": 5, "bytes_done": 100,
                      "current_files": ["/private/credential-file"],
                      "error": "private diagnostic", "password": "not-for-display"}).encode() + b"\n"
    with observe_progress(events.append):
        handler = stdout_progress_handler("operation-1", "capture")
        assert handler is not None
        for fragment in (raw[:7], raw[7:42], raw[42:]):
            handler(fragment)
    assert events == [{"operation_id": "operation-1", "label": "capture",
                       "message_type": "status", "percent_done": 0.5,
                       "total_files": 10, "files_done": 5, "bytes_done": 100}]


def test_oversized_and_malformed_output_does_not_poison_following_progress():
    events = []
    with observe_progress(events.append):
        handler = stdout_progress_handler("operation-2", "restore")
        assert handler is not None
        handler(b"x" * (64 * 1024))
        handler(b"untrusted remainder\ninvalid-json\n")
        handler(b'{"message_type":"status","percent_done":NaN,"files_done":true}\n')
        handler(b'{"message_type":"status","percent_done":' + b"9" * 1000 + b'}\n')
        handler(b'{"message_type":"summary","total_files_processed":12,"total_bytes_processed":240}\n')
    assert events == [{"operation_id": "operation-2", "label": "restore",
                       "message_type": "summary", "total_files_processed": 12,
                       "total_bytes_processed": 240}]
