#!/usr/bin/env bash
DB_PATH="${OPENCLAW_HOME:-$HOME/.openclaw}/article_history.db"
ACTIVE_URL_FILE="/tmp/openclaw_active_url.txt"

# Initialize DB
sqlite3 "$DB_PATH" "CREATE TABLE IF NOT EXISTS history (url TEXT PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);"

# Clean up older than 7 days automatically
sqlite3 "$DB_PATH" "DELETE FROM history WHERE timestamp <= datetime('now', '-7 days');"

case "$1" in
    check)
        URL=$2
        SAFE_URL=$(echo "$URL" | sed "s/'/''/g")
        # EXACT match verification
        COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM history WHERE url = '${SAFE_URL}';")
        if [ "$COUNT" -gt 0 ]; then
            echo "EXISTS"
        else
            # Save it to the side-channel file so WP-Publisher can find it later!
            echo "$URL" > "$ACTIVE_URL_FILE"
            echo "NOT_FOUND"
        fi
        ;;
    add)
        URL=$2
        CLEAN_URL=$(echo "$URL" | sed "s/'/''/g")
        if [ -n "$CLEAN_URL" ]; then
            sqlite3 "$DB_PATH" "INSERT OR IGNORE INTO history (url) VALUES ('${CLEAN_URL}');"
        fi
        echo "ADDED"
        ;;
    *)
        echo "Usage: $0 check <url> | add <url>"
        ;;
esac
