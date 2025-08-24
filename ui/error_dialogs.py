from typing import Optional
from PyQt6.QtWidgets import (QMessageBox, QDialog, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QTextEdit, QCheckBox)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QIcon

class ErrorDialog(QMessageBox):
    """User-friendly error dialog with expandable technical details."""
    
    def __init__(self, parent=None, title: str = "Error", message: str = "", 
                 details: str = "", error_type: str = "error"):
        super().__init__(parent)
        
        # Set dialog properties
        self.setWindowTitle(title)
        self.setText(message)
        
        # Set appropriate icon based on error type
        if error_type == "warning":
            self.setIcon(QMessageBox.Icon.Warning)
        elif error_type == "info":
            self.setIcon(QMessageBox.Icon.Information)
        else:
            self.setIcon(QMessageBox.Icon.Critical)
        
        # Add technical details if provided
        if details:
            self.setDetailedText(f"Technical details:\n{details}")
        
        # Set standard buttons
        self.setStandardButtons(QMessageBox.StandardButton.Ok)
        self.setDefaultButton(QMessageBox.StandardButton.Ok)

class RetryDialog(QDialog):
    """Dialog with retry functionality for recoverable errors."""
    
    def __init__(self, parent=None, title: str = "Connection Error", 
                 message: str = "", details: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(400, 200)
        
        layout = QVBoxLayout(self)
        
        # Main message
        message_label = QLabel(message)
        message_label.setWordWrap(True)
        layout.addWidget(message_label)
        
        # Technical details (expandable)
        if details:
            self.details_text = QTextEdit()
            self.details_text.setPlainText(details)
            self.details_text.setMaximumHeight(100)
            self.details_text.hide()
            layout.addWidget(self.details_text)
            
            self.show_details_btn = QPushButton("Show Details")
            self.show_details_btn.clicked.connect(self._toggle_details)
            layout.addWidget(self.show_details_btn)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        self.retry_btn = QPushButton("Retry")
        self.retry_btn.clicked.connect(self.accept)
        button_layout.addWidget(self.retry_btn)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(button_layout)
        
        # Set default button
        self.retry_btn.setDefault(True)
    
    def _toggle_details(self):
        """Toggle visibility of technical details."""
        if hasattr(self, 'details_text'):
            if self.details_text.isVisible():
                self.details_text.hide()
                self.show_details_btn.setText("Show Details")
                self.resize(400, 200)
            else:
                self.details_text.show()
                self.show_details_btn.setText("Hide Details")
                self.resize(400, 300)

class ErrorDialogManager:
    """Centralized error dialog management with consistent styling and behavior."""
    
    @staticmethod
    def show_media_file_error(parent, file_path: str, error_msg: str):
        """Show error for media file loading issues."""
        if "No such file" in error_msg or "does not exist" in error_msg:
            title = "File Not Found"
            message = f"The selected file could not be found:\n\n{file_path}\n\nThe file may have been moved, renamed, or deleted."
            suggestions = "• Check if the file still exists at the specified location\n• Try selecting the file again\n• Verify the file hasn't been moved to another folder"
        elif "format" in error_msg.lower() or "codec" in error_msg.lower():
            title = "Unsupported File Format"
            message = f"The selected file format is not supported or the file may be corrupted:\n\n{file_path}"
            suggestions = "• Ensure the file is a valid MP3 or MIDI file\n• Try converting the file to a supported format\n• Check if the file is corrupted by opening it in another application"
        elif "permission" in error_msg.lower():
            title = "File Access Error"
            message = f"Permission denied when trying to access:\n\n{file_path}"
            suggestions = "• Check that you have read permissions for this file\n• Close other applications that might be using the file\n• Try running the application as administrator"
        else:
            title = "Media File Error"
            message = f"An error occurred while loading the media file:\n\n{file_path}"
            suggestions = "• Verify the file is not corrupted\n• Try selecting a different file\n• Check the application logs for more details"
        
        full_message = f"{message}\n\nSuggestions:\n{suggestions}"
        
        dialog = ErrorDialog(parent, title, full_message, error_msg, "error")
        dialog.exec()
    
    @staticmethod
    def show_audio_hardware_error(parent, error_msg: str, error_type: str = "initialization"):
        """Show error for audio hardware issues."""
        if error_type == "initialization":
            title = "Audio System Error"
            message = "Unable to initialize the audio system. Audio playback and metronome will be disabled."
            suggestions = "• Check that your audio devices are properly connected\n• Verify audio drivers are installed and up to date\n• Try restarting the application\n• Check system audio settings"
        elif error_type == "device_not_found":
            title = "No Audio Device Found"
            message = "No audio output device was detected. The application will continue without audio."
            suggestions = "• Connect headphones or speakers to your computer\n• Check that audio devices are enabled in system settings\n• Try selecting a different audio device if available"
        else:
            title = "Audio Error"
            message = "An audio system error occurred. Some audio features may not work properly."
            suggestions = "• Try restarting the application\n• Check system audio settings\n• Verify audio device connections"
        
        full_message = f"{message}\n\nWhat you can try:\n{suggestions}"
        
        dialog = ErrorDialog(parent, title, full_message, error_msg, "warning")
        dialog.exec()
    
    @staticmethod
    def show_midi_connection_error(parent, port_name: str, error_msg: str) -> bool:
        """Show MIDI connection error with retry option. Returns True if user wants to retry."""
        title = "MIDI Connection Error"
        message = f"Unable to connect to MIDI port '{port_name}'.\n\nThis may be because:\n• The MIDI device is not connected\n• Another application is using the MIDI port\n• The MIDI drivers are not properly installed"
        suggestions = f"Technical details: {error_msg}"
        
        dialog = RetryDialog(parent, title, message, suggestions)
        return dialog.exec() == QDialog.DialogCode.Accepted
    
    @staticmethod
    def show_dmx_connection_error(parent, source_name: str, error_msg: str) -> bool:
        """Show DMX connection error with retry option. Returns True if user wants to retry."""
        if "Art-Net" in source_name:
            title = "Art-Net Connection Error"
            message = f"Unable to connect to Art-Net controller.\n\nPossible causes:\n• Network connection issues\n• DMX controller is offline\n• Incorrect IP address or port\n• Firewall blocking the connection"
        else:
            title = "DMX Interface Error"
            message = f"Unable to connect to DMX interface '{source_name}'.\n\nPossible causes:\n• DMX USB interface not connected\n• Interface drivers not installed\n• Interface in use by another application\n• USB port issues"
        
        dialog = RetryDialog(parent, title, message, error_msg)
        return dialog.exec() == QDialog.DialogCode.Accepted
    
    @staticmethod
    def show_configuration_error(parent, error_msg: str, can_reset: bool = True):
        """Show configuration error with reset option."""
        title = "Configuration Error"
        message = "A problem was detected with the application configuration.\n\nThe application will use default settings to continue running."
        
        if can_reset:
            message += "\n\nWould you like to reset the configuration to defaults? This will clear all your custom settings."
            
            msg_box = QMessageBox(parent)
            msg_box.setWindowTitle(title)
            msg_box.setText(message)
            msg_box.setDetailedText(f"Technical details:\n{error_msg}")
            msg_box.setIcon(QMessageBox.Icon.Warning)
            
            reset_btn = msg_box.addButton("Reset to Defaults", QMessageBox.ButtonRole.AcceptRole)
            continue_btn = msg_box.addButton("Continue with Defaults", QMessageBox.ButtonRole.RejectRole)
            msg_box.setDefaultButton(continue_btn)
            
            msg_box.exec()
            return msg_box.clickedButton() == reset_btn
        else:
            dialog = ErrorDialog(parent, title, message, error_msg, "warning")
            dialog.exec()
            return False
    
    @staticmethod
    def show_file_permission_error(parent, file_path: str, operation: str = "access"):
        """Show file permission error."""
        title = "File Permission Error"
        message = f"Unable to {operation} file:\n\n{file_path}\n\nThis may be due to insufficient permissions or the file being in use by another application."
        suggestions = "• Check file permissions\n• Close other applications that might be using the file\n• Try running as administrator\n• Verify the file is not read-only"
        
        full_message = f"{message}\n\nSuggestions:\n{suggestions}"
        
        dialog = ErrorDialog(parent, title, full_message, "", "error")
        dialog.exec()
    
    @staticmethod
    def show_validation_error(parent, field_name: str, value: str, expected: str):
        """Show validation error for user input."""
        title = "Invalid Input"
        message = f"The value entered for {field_name} is not valid.\n\nEntered: {value}\nExpected: {expected}"
        
        dialog = ErrorDialog(parent, title, message, "", "warning")
        dialog.exec()