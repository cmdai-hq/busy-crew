"""Synthetic Codex append benchmark and original-fold equivalence; no real agent data.

Usage: uv run python scripts/benchmark_codex_fold.py --baseline-root /tmp/busy-crew-baseline
Prepare that root with `git archive <baseline-commit> | tar -x -C /tmp/busy-crew-baseline`.
"""
import argparse
import importlib.util
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    from agentdeck.observe import Observer
    spec = importlib.util.spec_from_file_location('agentdeck.baseline_observe', args.baseline_root / 'agentdeck' / 'observe.py')
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)

    def event(kind, **values):
        return {'type': 'event_msg', 'payload': {'type': kind, **values}}

    # Independent original fold versus refactored fold over synthetic transitions.
    from agentdeck.observe import rollout_details
    random.seed(12345)
    for i in range(1000):
        records = [event('task_started', turn_id='turn')]
        for j in range(random.randrange(1, 100)):
            records.append(random.choice([
                event('approval_request'), event('approval_response'),
                event('thread_status_changed', active_flags=['waitingOnUserInput']),
                event('thread_status_changed', active_flags=[]),
                event('task_complete', turn_id='turn', completed_at=1000),
                event('turn_aborted', turn_id='turn'),
                event('task_started', turn_id='child', root_turn_id='turn'),
                event('task_started', turn_id='next')]))
        assert rollout_details(records, 'turn') == baseline.rollout_details(records, 'turn')
    print('Original fold equivalence: 1000 synthetic event sequences passed')

    for count in (1000, 50000):
        results = {}
        for name, cls in [('before', baseline.Observer), ('after', Observer)]:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runtime = root / 'runtime'
                runtime.mkdir()
                path = root / 'rollout'
                line = json.dumps(event('thread_status_changed', active_flags=[])) + '\n'
                path.write_text(json.dumps(event('task_started', turn_id='turn')) + '\n' + line * count)
                observer = cls(runtime, claude_root=root / 'claude', codex_root=root / 'codex')
                observer._turn_details('fixture', path, path.stat(), 'turn')
                times = []
                for _ in range(60):
                    with path.open('a') as handle:
                        handle.write(line)
                    at = time.process_time_ns()
                    assert observer._turn_details('fixture', path, path.stat(), 'turn')['waiting'] is False
                    times.append((time.process_time_ns() - at) / 1e6)
                results[name] = round(statistics.median(times), 4)
        print(json.dumps({'historical_records': count, 'cpu_ms_median': results, 'speedup': round(results['before']/results['after'],1)}))


if __name__ == "__main__":
    main()
