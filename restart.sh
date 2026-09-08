#!/bin/bash

BOT_NAME="school_discord_noti_bot.py"
PROJECT_DIR="$HOME/git/discord_notification_bot"
VENV="$PROJECT_DIR/main"
LOG="$PROJECT_DIR/bot.log"

cd "$PROJECT_DIR" || exit 1

echo "================================"
echo " Discord Bot Restart"
echo "================================"

# 1. 기존 봇 프로세스 종료
echo "[1/4] 기존 봇 종료..."

PIDS=$(pgrep -f "$BOT_NAME")

if [ -n "$PIDS" ]; then
    echo "기존 PID: $PIDS"
    kill $PIDS
    sleep 2
else
    echo "실행 중인 봇 없음"
fi

# 2. venv 활성화
echo "[2/4] venv 활성화..."

source "$VENV/bin/activate"

echo "Python: $(which python)"

# 3. 기존 로그 초기화 후 봇 실행
echo "[3/4] 봇 실행..."

> "$LOG"

nohup python "$BOT_NAME" >> "$LOG" 2>&1 &

NEW_PID=$!

echo "새 PID: $NEW_PID"

# 실행 직후 에러 확인을 위해 잠시 대기
sleep 2

if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "봇 실행 성공"
else
    echo "봇 실행 실패"
    cat "$LOG"
    exit 1
fi

# 4. 실시간 로그
echo "[4/4] 로그 출력 (Ctrl+C로 로그 보기 종료)"
echo "================================"

tail -f "$LOG"