#!/usr/bin/env python3
"""
dedup.py — OpenViking 知识库 AI 去重工具

功能：
1. 扫描所有 curated 资源，检测重复/重叠
2. AI 评判哪篇更好，或合并精华部分
3. 清理空/临时资源

用法:
  python3 dedup.py --scan                   # 扫描并报告重复
  python3 dedup.py --clean                  # AI 评判 + 清理
  python3 dedup.py --clean --dry            # 预览（不执行删除/入库）
  python3 dedup.py --clean --merge          # AI 合并重复为新文档
  python3 dedup.py --threshold 0.4          # 调整相似度阈值
"""
import os, sys, json, argparse, re, time, requests
from pathlib import Path
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_dotenv():
    env_file = Path(__file__).parent / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k, v = k.strip(), v.strip()
                if v:
                    os.environ[k] = v


def get_client():
    load_dotenv()
    cfg = os.environ.get("OPENVIKING_CONFIG_FILE",
        str(Path.home() / ".openviking" / "ov.conf"))
    dp = os.environ.get("CURATOR_DATA_PATH", str(Path.cwd() / "data"))
    os.environ["OPENVIKING_CONFIG_FILE"] = cfg
    import openviking as ov
    client = ov.SyncOpenViking(path=dp)
    client.initialize()
    return client


def chat(model, messages, timeout=60):
    """调用 LLM。"""
    base = os.environ.get("CURATOR_OAI_BASE", "")
    key = os.environ.get("CURATOR_OAI_KEY", "")
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "stream": False},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def get_judge_model():
    models = os.environ.get("CURATOR_JUDGE_MODELS", "【Claude Code】Claude-Sonnet 4-6")
    return models.split(",")[0].strip()


# ─── 资源读取 ─────────────────────────────────────────────

def read_resource_text(client, uri: str) -> str:
    try:
        content = client.read(uri)
        if content:
            return str(content)
    except:
        pass
    try:
        children = client.ls(uri)
        for child in children:
            name = child.get('name', '')
            if name.endswith('.md') and not name.startswith('.'):
                try:
                    return str(client.read(f"{uri}/{name}"))
                except:
                    pass
        for child in children:
            if child.get('name') == '.abstract.md':
                try:
                    return str(client.read(f"{uri}/.abstract.md"))
                except:
                    pass
    except:
        pass
    return ""


# ─── 相似度计算 ───────────────────────────────────────────

def extract_keywords(text: str) -> set:
    text_l = text.lower()
    en = set(re.findall(r'[a-z][a-z0-9_\-]{2,}', text_l))
    cn = set(re.findall(r'[\u4e00-\u9fff]{2,6}', text_l))
    return en | cn


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ka, kb = extract_keywords(a), extract_keywords(b)
    if not ka or not kb:
        return 0.0
    jaccard = len(ka & kb) / len(ka | kb)
    if jaccard < 0.3:
        return jaccard
    ratio = SequenceMatcher(None, a[:2000], b[:2000]).ratio()
    return 0.4 * jaccard + 0.6 * ratio


# ─── 扫描 ────────────────────────────────────────────────

def scan(client) -> dict:
    resources = client.ls('viking://resources')
    entries = {}
    for r in resources:
        name = r.get('name', '')
        if name.startswith('.'):
            continue
        uri = f'viking://resources/{name}'
        text = read_resource_text(client, uri)
        abstract = ""
        try:
            abstract = str(client.read(f"{uri}/.abstract.md"))
        except:
            pass
        entries[uri] = {
            'name': name,
            'text': text,
            'abstract': abstract,
            'keys': extract_keywords(text or abstract),
            'length': len(text),
        }
    return entries


def find_duplicates(entries: dict, threshold=0.55) -> list:
    uris = list(entries.keys())
    dupes = []
    for i in range(len(uris)):
        for j in range(i + 1, len(uris)):
            a, b = uris[i], uris[j]
            if 'tmp' in a or 'tmp' in b:
                continue
            sim = similarity(
                entries[a]['text'] or entries[a]['abstract'],
                entries[b]['text'] or entries[b]['abstract'],
            )
            if sim >= threshold:
                dupes.append({
                    'a': a, 'b': b,
                    'similarity': round(sim, 3),
                    'a_len': entries[a]['length'],
                    'b_len': entries[b]['length'],
                })
    dupes.sort(key=lambda x: x['similarity'], reverse=True)
    return dupes


def find_empty(entries: dict) -> list:
    empty = []
    for uri, info in entries.items():
        if info['length'] == 0 and not info['abstract']:
            empty.append(uri)
        elif info['name'].startswith('tmp'):
            empty.append(uri)
    return empty


# ─── AI 评判 ─────────────────────────────────────────────

def ai_judge(text_a: str, text_b: str, uri_a: str, uri_b: str) -> dict:
    """让 AI 评判两篇重复文档，返回决策。"""
    model = get_judge_model()
    prompt = f"""你是知识库质量审核员。下面有两篇内容高度重叠的文档，请评判：

## 文档 A ({uri_a})
{text_a[:3000]}

## 文档 B ({uri_b})
{text_b[:3000]}

请用严格 JSON 回答（不要 markdown 包裹）：
{{
  "winner": "A" | "B" | "merge",
  "reason": "简要说明选择理由（30字内）",
  "a_unique": ["A 独有的有价值信息点"],
  "b_unique": ["B 独有的有价值信息点"],
  "quality_a": 1-10,
  "quality_b": 1-10
}}

判断标准：
- 信息完整度（覆盖面广的更好）
- 准确性（有具体细节/代码/配置的更好）
- 组织性（结构清晰的更好）
- 如果两篇各有独特价值，选 "merge"
- 如果一篇明显更好，选它"""

    try:
        resp = chat(model, [{"role": "user", "content": prompt}], timeout=30)
        # 提取 JSON
        resp = resp.strip()
        if resp.startswith('```'):
            resp = re.sub(r'^```\w*\n?', '', resp)
            resp = re.sub(r'\n?```$', '', resp)
        return json.loads(resp)
    except Exception as e:
        return {"winner": "longer", "reason": f"AI judge failed: {e}", "quality_a": 0, "quality_b": 0}


def ai_merge(text_a: str, text_b: str, uri_a: str, uri_b: str) -> str:
    """让 AI 合并两篇文档的精华。"""
    model = get_judge_model()
    prompt = f"""你是知识库编辑。请将下面两篇重叠文档合并为一篇高质量文档。

要求：
- 保留两篇中所有独特且有价值的信息
- 去掉重复内容
- 统一格式（Markdown）
- 保持简洁，不要添加原文没有的内容
- 注明关键信息的来源

## 文档 A ({uri_a})
{text_a[:4000]}

## 文档 B ({uri_b})
{text_b[:4000]}

直接输出合并后的 Markdown 文档（不要额外说明）："""

    try:
        return chat(model, [{"role": "user", "content": prompt}], timeout=60)
    except Exception as e:
        return ""


# ─── 清理 ────────────────────────────────────────────────

def clean_resource(client, uri: str, dry=False):
    if dry:
        print(f"  [DRY] would delete {uri}")
        return True
    try:
        try:
            children = client.ls(uri)
            for child in children:
                name = child.get('name', '')
                if name:
                    try:
                        client.rm(f"{uri}/{name}")
                    except:
                        pass
        except:
            pass
        client.rm(uri)
        print(f"  ✅ deleted {uri}")
        return True
    except Exception as e:
        # fallback: 直接删文件系统
        dp = os.environ.get("CURATOR_DATA_PATH", "data")
        name = uri.split("/")[-1]
        fs_path = Path(dp) / "viking" / "resources" / name
        if fs_path.exists():
            import shutil
            shutil.rmtree(fs_path)
            print(f"  ✅ deleted {uri} (via filesystem)")
            return True
        print(f"  ❌ failed: {e}")
        return False


def ingest_merged(client, markdown: str) -> str:
    """入库合并后的文档。"""
    try:
        result = client.add_resource(markdown)
        uri = result.get('root_uri', '') if isinstance(result, dict) else str(result)
        return uri
    except Exception as e:
        print(f"  ❌ ingest failed: {e}")
        return ""


# ─── 主流程 ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='OpenViking Curator AI dedup')
    parser.add_argument('--scan', action='store_true', help='Scan and report')
    parser.add_argument('--clean', action='store_true', help='AI judge + clean')
    parser.add_argument('--merge', action='store_true', help='Merge duplicates (with --clean)')
    parser.add_argument('--dry', action='store_true', help='Dry run')
    parser.add_argument('--threshold', type=float, default=0.55)
    args = parser.parse_args()

    if not args.scan and not args.clean:
        args.scan = True

    client = get_client()
    try:
        print("📊 扫描资源...")
        entries = scan(client)
        print(f"   共 {len(entries)} 个资源\n")

        # 空/tmp
        empty = find_empty(entries)
        if empty:
            print(f"🗑️  空/临时资源 ({len(empty)}):")
            for u in empty:
                print(f"   {u}")
            print()

        # 重复
        dupes = find_duplicates(entries, threshold=args.threshold)
        if dupes:
            print(f"🔁 疑似重复 ({len(dupes)} 对, threshold={args.threshold}):")
            for d in dupes:
                print(f"   {d['similarity']:.0%} | {d['a']} ({d['a_len']}字)")
                print(f"        ↔ {d['b']} ({d['b_len']}字)")
            print()
        else:
            print("✅ 未发现重复\n")

        if args.clean:
            # 清空/tmp
            if empty:
                print("🧹 清理空/临时资源...")
                for u in empty:
                    clean_resource(client, u, dry=args.dry)
                print()

            # AI 评判重复
            if dupes:
                deleted = set()
                print("🤖 AI 评判重复资源...\n")
                for d in dupes:
                    if d['a'] in deleted or d['b'] in deleted:
                        continue

                    a_text = entries[d['a']]['text'] or entries[d['a']]['abstract']
                    b_text = entries[d['b']]['text'] or entries[d['b']]['abstract']

                    print(f"  📋 比较: {d['a']} vs {d['b']} (相似度 {d['similarity']:.0%})")

                    judgment = ai_judge(a_text, b_text, d['a'], d['b'])
                    winner = judgment.get('winner', 'longer')
                    reason = judgment.get('reason', '')
                    qa = judgment.get('quality_a', '?')
                    qb = judgment.get('quality_b', '?')

                    print(f"     质量: A={qa}/10, B={qb}/10")
                    print(f"     判定: {winner} — {reason}")

                    if judgment.get('a_unique'):
                        print(f"     A 独有: {', '.join(judgment['a_unique'][:3])}")
                    if judgment.get('b_unique'):
                        print(f"     B 独有: {', '.join(judgment['b_unique'][:3])}")

                    if winner == 'merge' and args.merge:
                        # AI 合并
                        print(f"     🔀 合并中...")
                        merged = ai_merge(a_text, b_text, d['a'], d['b'])
                        if merged and not args.dry:
                            new_uri = ingest_merged(client, merged)
                            if new_uri:
                                print(f"     ✅ 新文档: {new_uri}")
                                clean_resource(client, d['a'], dry=args.dry)
                                clean_resource(client, d['b'], dry=args.dry)
                                deleted.add(d['a'])
                                deleted.add(d['b'])
                        elif args.dry:
                            print(f"     [DRY] would merge and replace both")
                    elif winner == 'merge' and not args.merge:
                        print(f"     💡 建议合并，加 --merge 参数启用")
                    elif winner in ('A', 'a'):
                        print(f"     保留 A: {d['a']}")
                        clean_resource(client, d['b'], dry=args.dry)
                        deleted.add(d['b'])
                    elif winner in ('B', 'b'):
                        print(f"     保留 B: {d['b']}")
                        clean_resource(client, d['a'], dry=args.dry)
                        deleted.add(d['a'])
                    else:
                        # fallback: 保留长的
                        to_del = d['b'] if d['a_len'] >= d['b_len'] else d['a']
                        to_keep = d['a'] if to_del == d['b'] else d['b']
                        print(f"     fallback: 保留较长 {to_keep}")
                        clean_resource(client, to_del, dry=args.dry)
                        deleted.add(to_del)

                    print()
                    time.sleep(1)  # 避免 API 限流

        if args.scan and not args.clean:
            if empty or dupes:
                print("💡 运行 `python3 dedup.py --clean` 启用 AI 评判")
                print("   加 --merge 允许合并重复文档")
    finally:
        try:
            client.close()
        except:
            pass


if __name__ == '__main__':
    main()
