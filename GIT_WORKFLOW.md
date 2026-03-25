# Git Workflow Guide

## Quick Reference

### Checking Status
```bash
git status          # See what's changed
git log --oneline   # See commit history
git diff            # See detailed changes
```

### Making Changes
```bash
# Stage specific files
git add src/trading_bot/backtester.py
git add tests/

# Stage all changes
git add -A

# View what will be committed
git status
```

### Creating Commits
```bash
# Simple commit
git commit -m "Your commit message"

# Detailed commit with description
git commit -m "feat: add new feature" -m "This is a detailed description of the changes"
```

### Commit Message Format
```
<type>: <short description (50 chars)>

<optional detailed description (wrap at 72 chars)>

Co-Authored-By: Name <email@example.com>
```

**Types:** feat, fix, docs, style, refactor, test, chore

### Examples

**Feature Commit:**
```bash
git commit -m "feat: add ADX filter to signal generation" -m "
- Add ADX > 25 filter to trending regime detection
- Only generate signals in confirmed trends
- Expected 5-10% improvement in win rate

Tested with 12-month backtest."
```

**Bug Fix:**
```bash
git commit -m "fix: correct fee calculation in backtester" -m "
Was counting only entry fee, not round-trip.
Now correctly calculates 0.14% (maker + taker)."
```

**Test Commit:**
```bash
git commit -m "test: add regression tests for ADX multiplier" -m "
Ensures ADX multiplier always stays >= 0.3.
Prevents negative contributions to score."
```

### Branching (for team projects)

```bash
# Create and switch to new branch
git checkout -b feature/my-feature

# Make changes, commit
git commit -m "feat: my new feature"

# Switch back to main
git checkout main

# Update main from origin (if connected to remote)
git pull origin main

# Merge feature branch into main
git merge feature/my-feature

# Delete feature branch
git branch -d feature/my-feature
```

### Undoing Changes

```bash
# Undo unstaged changes to a file
git checkout -- file.py

# Unstage a file (but keep changes)
git reset HEAD file.py

# View a previous commit
git show 1fd8248

# Revert to a previous commit (creates new commit)
git revert 1fd8248
```

### Connecting to GitHub (Optional)

```bash
# Add remote repository
git remote add origin https://github.com/yourusername/trading-bot.git

# Push to GitHub
git push -u origin main

# Pull from GitHub
git pull origin main

# See remotes
git remote -v
```

## Development Workflow

### 1. Start Work on New Feature
```bash
git checkout -b feature/your-feature
```

### 2. Make Changes and Test
```bash
# Edit files
# Run tests
python3 -m pytest tests/ -v

# Run backtest
python3 -m trading_bot.backtester --months 12
```

### 3. Commit Your Work
```bash
git add .
git commit -m "feat: your feature" -m "Detailed description"
```

### 4. Review Changes
```bash
git log -1 --stat
git show
```

### 5. Push to Remote (if using GitHub)
```bash
git push origin feature/your-feature
```

## Current Repository Status

**Commit:** 1fd8248
**Branch:** main
**Status:** Clean (no uncommitted changes)
**Files Tracked:** 20

## Important Files

- `.gitignore` — What to exclude from git
- `LICENSE` — MIT License
- `README.md` — Main documentation
- `docs/ARCHITECTURE.md` — Technical details
- `requirements.txt` — Python dependencies

## Sensitive Files (Git-Ignored)

These are excluded from version control:
- `config/binance-real.json` — API keys
- `config/telegram.json` — Bot tokens
- `__pycache__/` — Python cache
- `.pytest_cache/` — Test cache
- `*.egg-info/` — Package metadata

**⚠️ IMPORTANT:** Never commit API keys, tokens, or credentials!

## Tips

1. **Commit Often** — Small, focused commits are easier to review
2. **Write Clear Messages** — Explain the "why", not just the "what"
3. **Test Before Commit** — Run tests before committing changes
4. **Review Before Pushing** — Use `git diff` and `git status`
5. **Keep Master Clean** — Only production-ready code on main

## Troubleshooting

### "I committed something by mistake"
```bash
# Undo last commit but keep changes
git reset --soft HEAD~1
```

### "I need to change the last commit message"
```bash
git commit --amend -m "New message"
```

### "I want to see what changed in a commit"
```bash
git show <commit-hash>
git diff HEAD~1..HEAD
```

### "I accidentally deleted a file"
```bash
git checkout -- filename
git checkout HEAD -- filename  # from specific commit
```

## Resources

- [Git Documentation](https://git-scm.com/doc)
- [Commit Message Best Practices](https://chris.beams.io/posts/git-commit/)
- [GitHub Flow Guide](https://guides.github.com/introduction/flow/)
