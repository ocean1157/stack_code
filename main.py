"""Quant Scope 项目唯一总入口。

直接运行此文件只启动只读 Web 服务。数据采集和模型写库必须通过显式命令执行。
"""

from quant_system.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
