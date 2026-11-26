"""由TaskManager在11:00至21:00整点调用的真实温调入口。"""
from pathlib import Path
import sys

# 工作目录是scripts，显式加入项目根目录以复用已验证登录客户端。
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hangxin_hourly_temperature import main

if __name__=='__main__':sys.exit(main(['--execute']))
