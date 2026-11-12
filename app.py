import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from raw_store import RawStore


def demo(store):
    observed_at = datetime.now(timezone.utc).isoformat()
    from text_source import extract
    result=[]
    for entry in json.loads(Path('fixtures/manifest.json').read_text()):
        raw=Path(entry['path']).read_bytes()
        blocks=extract(raw.decode())
        digest=store.save(entry['id'],raw,{'site':entry['site'],'blocks':len(blocks)},observed_at)
        result.append({'文章':entry['id'],'块数':len(blocks),'保留文本长度':sum(len(block['text']) for block in blocks),'原文摘要':digest})
    return {'文本块':result,'说明':'仍包含导航与页脚，没有相似分组'}


def main():
    parser = argparse.ArgumentParser(description='本地原文采集演示')
    parser.add_argument('command', choices=['demo'])
    parser.add_argument('--output', default='output/raw.sqlite3')
    args = parser.parse_args()
    store = RawStore(args.output)
    try:
        result = demo(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
