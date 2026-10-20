"""Read local, bounded YAML definitions; no device access or control."""
from pathlib import Path
import yaml
from .workout import Workout, load_workout


def local_workouts(directory: Path, maximum_w: int) -> tuple[dict[str, Workout], list[str]]:
    workouts: dict[str, Workout] = {}
    errors: list[str] = []
    if not directory.exists():
        return workouts, errors
    if not directory.is_dir():
        return workouts, ['Workout-Verzeichnis ist kein Ordner.']
    try:
        paths = sorted(directory.iterdir())
    except OSError:
        return workouts, ['Workout-Verzeichnis nicht lesbar.']
    for path in paths:
        if path.suffix.lower() not in ('.yaml', '.yml') or not path.is_file():
            continue
        try:
            workout = load_workout(path)
            targets = [block.target.resolve(None) for block in workout.blocks]
            if targets[0] > min(50, maximum_w) or any(target > maximum_w for target in targets):
                raise ValueError('target outside GUI limits')
            workouts[f'file:{path.name}'] = workout
        except (ValueError, TypeError, OSError, yaml.YAMLError):
            errors.append(f'{path.name}: nicht geladen. YAML, absolute Wattwerte, Start ≤50 W und Trainingslimit prüfen.')
    return workouts, errors
