# TupperDMX Simulator Build System
# Cross-platform build automation integrated with uv

.PHONY: help install dev build clean build-windows build-macos build-linux test lint

# Default target
help:
	@echo "TupperDMX Simulator Build System"
	@echo ""
	@echo "Available commands:"
	@echo "  install        - Install dependencies using uv"
	@echo "  dev            - Install development dependencies"
	@echo "  build          - Build executable for current platform"
	@echo "  build-all      - Build for all platforms (requires cross-platform setup)"
	@echo "  build-windows  - Build Windows executable and installer"
	@echo "  build-macos    - Build macOS app bundle and DMG"
	@echo "  build-linux    - Build Linux executable"
	@echo "  clean          - Clean build artifacts"
	@echo "  test           - Run tests (if available)"
	@echo "  lint           - Run code linting"
	@echo "  run            - Run the application in development mode"

# Development setup
install:
	uv sync

dev:
	uv sync --extra build
	chmod +x scripts/build.py

# Build commands
build:
	uv run python scripts/build.py

build-all: build-windows build-macos build-linux

build-windows:
	@echo "Building for Windows..."
	uv run python scripts/build.py
	@if command -v makensis >/dev/null 2>&1; then \
		echo "Creating Windows installer..."; \
		makensis scripts/windows_installer.nsi; \
	else \
		echo "NSIS not found - skipping installer creation"; \
	fi

build-macos:
	@echo "Building for macOS..."
	uv run python scripts/build.py
	@if [ -d "dist/TupperDMX-Simulator.app" ]; then \
		echo "Creating DMG..."; \
		hdiutil create -srcfolder "dist/TupperDMX-Simulator.app" \
			-volname "TupperDMX Simulator" "dist/TupperDMX-Simulator.dmg"; \
	fi

build-linux:
	@echo "Building for Linux..."
	uv run python scripts/build.py
	@echo "Consider creating AppImage or Flatpak for distribution"

# Utility commands
clean:
	rm -rf build/ dist/ __pycache__/
	find . -name "*.pyc" -delete
	find . -name "*.pyo" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} +

test:
	@if [ -f "pytest.ini" ] || [ -f "tests/" ]; then \
		uv run pytest; \
	else \
		echo "No tests found"; \
	fi

lint:
	@if command -v ruff >/dev/null 2>&1; then \
		uv run ruff check .; \
	elif command -v flake8 >/dev/null 2>&1; then \
		uv run flake8 .; \
	else \
		echo "No linter found - install ruff or flake8"; \
	fi

run:
	uv run python main.py

# CI/CD helpers
ci-build:
	$(MAKE) dev
	$(MAKE) build
	@echo "Build artifacts:"
	@ls -la dist/