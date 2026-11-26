"""把交付模型原有离线用例纳入项目回归，临时导入路径不影响部署代码。"""
import importlib.util
from pathlib import Path
import sys


def load_tests(loader,tests,pattern):
    directory=Path(__file__).parent/'algorithms/hangxin_start_forecast_mean'
    spec=importlib.util.spec_from_file_location('hangxin_delivered_forecast_tests',directory/'test_forecast.py')
    module=importlib.util.module_from_spec(spec)
    sys.path.insert(0,str(directory))
    try:spec.loader.exec_module(module)
    finally:sys.path.remove(str(directory))
    return loader.loadTestsFromModule(module)
