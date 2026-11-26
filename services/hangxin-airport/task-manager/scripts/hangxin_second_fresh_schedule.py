"""受管入口：每五分钟按主机开关机表同步两段新风的联动时间。"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hangxin_second_fresh_schedule import main
if __name__=='__main__':raise SystemExit(main(['--execute']))
