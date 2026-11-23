"""`python -m src.studio_job_worker` 入口（阶段5·点2 拆分：行为与单文件时代逐行等价）。"""

import json
import os
import sys
import traceback
from pathlib import Path

from src.studio_runtime import terminate_with_parent

from src.studio_job_worker import main, process_job

if __name__ == "__main__":
    if sys.argv[1:] == ['--job']:
        terminate_with_parent(int(os.environ['STUDIO_WORKER_PARENT_PID']))
        try:
            process_job(json.load(sys.stdin))
        except Exception as exc:
            error_file = os.getenv('STUDIO_JOB_ERROR_FILE')
            if error_file:
                Path(error_file).write_text(str(exc)[:400], encoding='utf-8')
            traceback.print_exc()
            sys.exit(1)
    else:
        import signal
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        sys.exit(main())
