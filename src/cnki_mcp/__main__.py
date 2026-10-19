from __future__ import annotations

import argparse
import json
import sys

from .config import Settings


def main():
    parser = argparse.ArgumentParser(description="CNKI literature MCP (local stdio)")
    parser.add_argument("--config", help="本地 JSON 配置路径，也可使用 CNKI_MCP_CONFIG")
    parser.add_argument("--check", action="store_true", help="校验配置并报告能力，不启动浏览器或联网")
    arguments = parser.parse_args()
    try:
        settings = Settings.load(arguments.config)
    except Exception:
        print("配置加载失败；请检查文件、字段、许可依据、时间与主机列表。", file=sys.stderr)
        raise SystemExit(2) from None
    if arguments.check:
        print(json.dumps({"valid": True, "data_dir": str(settings.data_dir),
                          "institutions": [p.institution_id for p in settings.institutions],
                          "client": settings.client.model_dump(), "transport": "stdio"}, ensure_ascii=False, indent=2))
        return
    from .server import create_server

    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
