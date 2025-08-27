#!/usr/bin/env python3
"""
Cross-platform build script for TupperDMX Simulator
Integrates with uv for dependency management
"""

import os
import sys
import shutil
import subprocess
import platform
from pathlib import Path

class Builder:
    def __init__(self):
        self.project_root = Path(__file__).parent.parent
        self.dist_dir = self.project_root / "dist"
        self.build_dir = self.project_root / "build"
        self.platform = platform.system().lower()
        
    def clean(self):
        """Clean previous build artifacts"""
        print("🧹 Cleaning previous builds...")
        for dir_path in [self.dist_dir, self.build_dir]:
            if dir_path.exists():
                shutil.rmtree(dir_path)
                print(f"   Removed {dir_path}")
                
    def setup_build_env(self):
        """Install build dependencies using uv"""
        print("📦 Setting up build environment...")
        try:
            subprocess.run([
                "uv", "sync", "--extra", "build"
            ], check=True, cwd=self.project_root)
            print("   ✅ Build dependencies installed")
        except subprocess.CalledProcessError as e:
            print(f"   ❌ Failed to install build dependencies: {e}")
            sys.exit(1)
            
    def create_icons(self):
        """Create platform-specific icon files from PNG"""
        print("🎨 Creating platform-specific icons...")
        png_icon = self.project_root / "assets" / "icons" / "metronome_icon.png"
        
        if not png_icon.exists():
            print("   ⚠️  PNG icon not found, skipping icon creation")
            return
            
        try:
            if self.platform == "windows":
                # Create ICO file for Windows
                ico_path = png_icon.with_suffix('.ico')
                subprocess.run([
                    "uv", "run", "python", "-c",
                    f"from PIL import Image; img=Image.open('{png_icon}'); img.save('{ico_path}')"
                ], check=True, cwd=self.project_root)
                print("   ✅ Created Windows ICO icon")
                
            elif self.platform == "darwin":
                # Create ICNS file for macOS  
                icns_path = png_icon.with_suffix('.icns')
                subprocess.run([
                    "sips", "-s", "format", "icns", str(png_icon), "--out", str(icns_path)
                ], check=True)
                print("   ✅ Created macOS ICNS icon")
                
        except (subprocess.CalledProcessError, ImportError) as e:
            print(f"   ⚠️  Icon conversion failed: {e}")
            
    def build_executable(self):
        """Build executable using PyInstaller"""
        print("🔨 Building executable...")
        
        try:
            subprocess.run([
                "uv", "run", "pyinstaller", 
                "--clean",
                "--noconfirm",
                "build.spec"
            ], check=True, cwd=self.project_root)
            print("   ✅ Executable built successfully")
        except subprocess.CalledProcessError as e:
            print(f"   ❌ Build failed: {e}")
            sys.exit(1)
            
    def create_installer(self):
        """Create platform-specific installer"""
        print(f"📦 Creating {self.platform} installer...")
        if self.platform == "windows":
            self._create_windows_installer()
        elif self.platform == "darwin":
            self._create_macos_dmg()
        elif self.platform == "linux":
            self._create_linux_appimage()
        else:
            print(f"   ℹ️  No installer creation configured for {self.platform}")
            
    def _create_windows_installer(self):
        """Create Windows NSIS installer"""
        print("📦 Creating Windows installer...")
        nsis_script = self.project_root / "scripts" / "windows_installer.nsi"
        
        if not nsis_script.exists():
            print("   ⚠️  NSIS script not found, skipping installer creation")
            return
            
        try:
            subprocess.run([
                "makensis", str(nsis_script)
            ], check=True)
            print("   ✅ Windows installer created")
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("   ⚠️  NSIS not found, skipping installer creation")
            
    def _create_macos_dmg(self):
        """Create macOS DMG package"""
        print("📦 Creating macOS DMG...")
        app_path = self.dist_dir / "TupperDMX-Simulator.app"
        dmg_path = self.dist_dir / "TupperDMX-Simulator.dmg"
        
        if not app_path.exists():
            print("   ❌ App bundle not found")
            return
            
        try:
            subprocess.run([
                "hdiutil", "create", 
                "-srcfolder", str(app_path),
                "-volname", "TupperDMX Simulator",
                str(dmg_path)
            ], check=True)
            print("   ✅ macOS DMG created")
        except subprocess.CalledProcessError:
            print("   ⚠️  DMG creation failed")
            
    def _create_linux_appimage(self):
        """Create Linux AppImage"""
        print("📦 Creating Linux AppImage...")
        print("   ℹ️  AppImage creation requires additional setup - see documentation")
        
    def run(self, clean_first=True):
        """Run the complete build process"""
        print(f"🚀 Starting build for {self.platform}...")
        
        if clean_first:
            self.clean()
            
        self.setup_build_env()
        self.create_icons()
        self.build_executable()
        self.create_installer()
        
        print("✅ Build complete!")
        print(f"📁 Output directory: {self.dist_dir}")

def main():
    builder = Builder()
    
    # Parse command line arguments
    clean = "--no-clean" not in sys.argv
    
    builder.run(clean_first=clean)

if __name__ == "__main__":
    main()