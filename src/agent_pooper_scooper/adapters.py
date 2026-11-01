"""Public, local-only adapter seams. No live provider or notification access."""
import os
import stat

from . import disk, schema

MAX_EVIDENCE_BYTES = 2 * 1024 * 1024


class NullEvidenceAdapter:
    basis = "unavailable"

    def snapshot(self):
        return None


class FileEvidenceAdapter:
    """Read an explicit operator snapshot; completeness claims are assertions."""
    basis = "operator_asserted_file"

    def __init__(self, path):
        self.path = path

    def snapshot(self):
        try:
            parent, name = disk._open_parent(self.path)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            finally:
                os.close(parent)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise schema.ValidationError(["evidence must be a regular file"])
                text = handle.read(MAX_EVIDENCE_BYTES + 1)
        except OSError:
            return None
        except UnicodeError:
            raise schema.ValidationError(["evidence must be UTF-8"]) from None
        if len(text.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise schema.ValidationError(["evidence exceeds size limit"])
        return schema.load_planner_evidence_text(text)


class NullNotificationAdapter:
    def status(self):
        return {"status": "not_attempted"}
