import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import backups, db
parser=argparse.ArgumentParser()
parser.add_argument("action",choices=["create","restore"])
parser.add_argument("--source")
parser.add_argument("--destination")
args=parser.parse_args()
if args.action=="create":
    db.init_db()
    print(backups.create_bundle())
else:
    if not args.source or not args.destination:
        parser.error("restore requires --source and --destination (empty offline directory)")
    print(backups.restore_bundle(args.source,args.destination))
