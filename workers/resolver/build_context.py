"""Generate one reviewed build context: python -m workers.resolver.build_context MODE OUTPUT_DIRECTORY."""
import argparse
import shutil
from pathlib import Path

from labdeploy import scaffold


def generate(mode, output):
    commands = {}
    for version in scaffold.PYTHONS:
        commands[f"python-{version}-compile"] = scaffold.lock_container(version)
        commands[f"python-{version}-export"] = scaffold.export_container(version)
        commands[f"python-{version}-export-dev"] = scaffold.export_container(version, True)
    for version in scaffold.NODE_IMAGES:
        commands[f"node-{version}-lock"] = scaffold.node_lock_container(version)
    if mode not in commands:
        raise ValueError("Unknown resolver mode")
    args = commands[mode]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(Path(__file__).with_name("worker.py"), output / "worker.py")
    (output / "mode").write_text(mode + "\n")
    (output / "command.sh").write_text(args[-1] + "\n")
    extra = "RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*\n" if mode.startswith("node-") else ""
    (output / "Dockerfile").write_text(f"FROM {args[-4]}\n" + extra + "WORKDIR /worker\nCOPY worker.py mode command.sh ./\n"
                                     "USER 65532:65532\nWORKDIR /tmp\nENTRYPOINT [\"python3\", \"/worker/worker.py\"]\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode")
    parser.add_argument("output")
    arguments = parser.parse_args()
    generate(arguments.mode, arguments.output)
