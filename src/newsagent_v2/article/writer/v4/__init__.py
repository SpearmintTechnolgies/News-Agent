"""NewsAgent V4 natural-prose generation boundary. V3 modules remain intact."""

from newsagent_v2.article.writer.v4.compile import V4CompileResult, compile_v4_article
from newsagent_v2.article.writer.v4.experiment import run_v4_experiment
from newsagent_v2.article.writer.v4.packet import WriterEvidencePacket, build_writer_evidence_packet
from newsagent_v2.article.writer.v4.writer import (
    V4_WRITER_MODEL,
    V4NaturalProseWriter,
    FailoverV4Writer,
    ScriptedV4Writer,
    assert_v4_writer_is_free,
    build_v4_writer,
)

__all__ = [
    "V4CompileResult",
    "V4NaturalProseWriter",
    "FailoverV4Writer",
    "V4_WRITER_MODEL",
    "ScriptedV4Writer",
    "WriterEvidencePacket",
    "assert_v4_writer_is_free",
    "build_v4_writer",
    "build_writer_evidence_packet",
    "compile_v4_article",
    "run_v4_experiment",
]
