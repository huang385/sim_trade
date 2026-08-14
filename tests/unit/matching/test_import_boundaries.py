import subprocess
import sys


def test_importing_types_does_not_load_registry_or_vn_engine():
    """
    在全新解释器中验证包初始化没有引擎注册副作用。

    当前 pytest 进程可能已经由其他测试加载 Registry，因此必须使用子进程
    检查仅导入 app.matching.types 时的真实模块加载集合。
    """

    script = (
        "import sys\n"
        "import app.matching.types\n"
        "assert 'app.matching.registry' not in sys.modules\n"
        "assert 'app.matching.engines.vn' not in sys.modules\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
