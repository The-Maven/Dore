"""CLI structured (JSON) output."""
import json

from sca.cli import _emit_json
from sca.models import Metrics


def test_emit_json_serialises_dataclass(capsys):
    metrics = Metrics(
        attested_coverage=1.02,
        live_coverage=1.0,
        staleness_days=12,
        supply_drift=0.01,
    )
    _emit_json(metrics)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["attested_coverage"] == 1.02
    assert parsed["live_coverage"] == 1.0
    assert parsed["staleness_days"] == 12
