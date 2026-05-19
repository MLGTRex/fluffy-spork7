import asyncio
import sys
import os
from datetime import datetime


# Each stage is (display_name, relative_path_from_main.py).
# Paths reflect the current pre-reorganization structure.
PIPELINE_STAGES = [
    ("Deep Research", "deep research/runDeepResearch.py"),
    ("Debate Cases", "debate/runDebate.py"),
    ("Debate Rebuttals", "debate rebuttal/runDebateRebuttal.py"),
    ("Debate Synthesis", "debate synthesis/runDebateSynthesis.py"),
]


async def stream_and_capture(stream, output_buffer, terminal_stream):
    """Read subprocess stream line-by-line, forward to terminal, capture for failure dumps."""
    while True:
        line = await stream.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace")
        output_buffer.append(decoded)
        terminal_stream.write(decoded)
        terminal_stream.flush()


async def run_stage(stage_name: str, relative_script_path: str, stage_num: int, total_stages: int) -> bool:
    """Run a single pipeline stage as a subprocess from its own working directory."""
    project_root = os.path.dirname(os.path.abspath(__file__))
    script_full_path = os.path.join(project_root, relative_script_path)
    script_working_dir = os.path.dirname(script_full_path)
    
    print(f"\n{'#'*70}")
    print(f"# Stage {stage_num}/{total_stages}: {stage_name}")
    print(f"# Script: {relative_script_path}")
    print(f"# Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*70}\n", flush=True)
    
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        script_full_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=script_working_dir,
    )
    
    stdout_buffer = []
    stderr_buffer = []
    
    await asyncio.gather(
        stream_and_capture(process.stdout, stdout_buffer, sys.stdout),
        stream_and_capture(process.stderr, stderr_buffer, sys.stderr),
    )
    
    await process.wait()
    success = process.returncode == 0
    
    if success:
        print(f"\n[Stage {stage_num}/{total_stages}: {stage_name}] Completed successfully.")
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    else:
        print(f"\n[Stage {stage_num}/{total_stages}: {stage_name}] FAILED with exit code {process.returncode}.")
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    
    return success


async def main():
    print(f"\n{'='*70}")
    print(f"  Investment Pipeline — Full Run")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}", flush=True)
    
    results = []
    total_stages = len(PIPELINE_STAGES)
    
    for idx, (stage_name, relative_script_path) in enumerate(PIPELINE_STAGES, start=1):
        success = await run_stage(stage_name, relative_script_path, idx, total_stages)
        results.append((stage_name, success))
    
    print(f"\n{'='*70}")
    print(f"  Pipeline Summary")
    print(f"  Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")
    for stage_name, success in results:
        status = "OK" if success else "FAILED"
        print(f"  [{status}] {stage_name}")
    print(f"{'='*70}\n", flush=True)
    
    any_failed = any(not success for _, success in results)
    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    asyncio.run(main())