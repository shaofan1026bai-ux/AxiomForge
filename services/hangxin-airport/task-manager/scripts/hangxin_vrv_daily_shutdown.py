"""TaskManager每天北京时间00:00调用VRV全关策略。"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hangxin_vrv_daily_shutdown import main

if __name__=='__main__':raise SystemExit(main(['--execute']))
