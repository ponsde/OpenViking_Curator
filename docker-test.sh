#!/bin/bash
# docker-test.sh — 模拟新用户 Docker 环境验证
# 用法: bash docker-test.sh
set -e

echo "🧪 OpenViking Curator Docker 测试"
echo "================================="

# 1. 构建
echo ""
echo "📦 Step 1: 构建 Docker 镜像..."
docker compose build --quiet 2>&1

# 2. 健康检查
echo "🔍 Step 2: 健康检查..."
docker compose run --rm curator curator_query.py --status 2>&1

# 3. 路由测试 — 闲聊不路由
echo ""
echo "🚫 Step 3: 路由测试 — 闲聊应被拦截..."
RESULT=$(docker compose run --rm curator curator_query.py "你好" 2>&1)
echo "$RESULT"
if echo "$RESULT" | grep -q '"routed": false'; then
    echo "✅ 闲聊正确拦截"
else
    echo "❌ 闲聊未拦截"
    exit 1
fi

# 4. 查询测试 — 知识库查询
echo ""
echo "🔎 Step 4: 知识库查询..."
docker compose run --rm curator curator_query.py "Docker 部署最佳实践" 2>&1

# 5. MCP server 测试
echo ""
echo "🔌 Step 5: MCP server 测试..."
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' | \
    timeout 10 docker compose run --rm -T curator mcp_server.py 2>/dev/null | head -1

echo ""
echo "✅ 全部测试通过！"
