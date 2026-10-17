import asyncio
import json

from app.services.monitor_runner import run_due_monitors


def main():
    result = asyncio.run(run_due_monitors())

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
