"""
MIDI event editing dialog.

Provides a dialog interface for creating and editing MIDI events with
validation and user-friendly controls.
"""

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, 
                             QDoubleSpinBox, QSpinBox, QDialogButtonBox, QMessageBox)
from PyQt6.QtCore import Qt


class MidiEventEditDialog(QDialog):
    """Dialog for editing MIDI event properties."""
    
    def __init__(self, event_data, midi_note_map, is_new_event=False, parent=None):
        super().__init__(parent)
        self.event_data = event_data
        self.midi_note_map = midi_note_map
        self.is_new_event = is_new_event
        
        title = "Add MIDI Event" if is_new_event else "Edit MIDI Event Properties"
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(350, 200)
        
        self.setup_ui()
        self.populate_fields()
        
    def setup_ui(self):
        """Setup the dialog UI."""
        layout = QVBoxLayout(self)
        
        # Event Type
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("Event Type:"))
        
        self.type_combo = QComboBox()
        
        # For new events, add a "Select event type..." placeholder
        if self.is_new_event:
            self.type_combo.addItem("Select event type...", None)
        
        # Populate with available MIDI note types from config
        for note_str, info in self.midi_note_map.items():
            part_name = getattr(info, 'part', f'Note {note_str}')
            self.type_combo.addItem(part_name, note_str)
        type_layout.addWidget(self.type_combo)
        layout.addLayout(type_layout)
        
        # Time
        time_layout = QHBoxLayout()
        time_layout.addWidget(QLabel("Time (seconds):"))
        
        self.time_spin = QDoubleSpinBox()
        self.time_spin.setDecimals(3)
        self.time_spin.setMinimum(0.0)
        self.time_spin.setMaximum(999.0)
        self.time_spin.setSingleStep(0.1)
        time_layout.addWidget(self.time_spin)
        layout.addLayout(time_layout)
        
        # Velocity
        velocity_layout = QHBoxLayout()
        velocity_layout.addWidget(QLabel("Velocity:"))
        
        self.velocity_spin = QSpinBox()
        self.velocity_spin.setMinimum(0)
        self.velocity_spin.setMaximum(127)
        velocity_layout.addWidget(self.velocity_spin)
        layout.addLayout(velocity_layout)
        
        # Buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        
        self.button_box.accepted.connect(self.validate_and_accept)
        self.button_box.rejected.connect(self.reject)
        
        # Connect combo box change to validation for new events
        if self.is_new_event:
            self.type_combo.currentTextChanged.connect(self.validate_form)
            
        layout.addWidget(self.button_box)
        
    def populate_fields(self):
        """Populate dialog fields with current values."""
        if self.is_new_event:
            # For new events, set defaults
            self.time_spin.setValue(self.event_data.get('time', 0.0))
            self.velocity_spin.setValue(self.event_data.get('velocity', 127))
            # Keep the "Select event type..." selected (index 0)
            self.validate_form()  # Initial validation
        else:
            # Set current values for existing events
            current_note = str(self.event_data.get('note', ''))
            
            # Find and select current note type in combo box
            for i in range(self.type_combo.count()):
                if self.type_combo.itemData(i) == current_note:
                    self.type_combo.setCurrentIndex(i)
                    break
                    
            self.time_spin.setValue(self.event_data.get('time', 0.0))
            self.velocity_spin.setValue(self.event_data.get('velocity', 64))
            
    def validate_form(self):
        """Validate the form and enable/disable OK button."""
        if self.is_new_event:
            # For new events, ensure a valid event type is selected
            selected_data = self.type_combo.currentData()
            is_valid = selected_data is not None
            self.ok_button.setEnabled(is_valid)
        else:
            # For existing events, always valid
            self.ok_button.setEnabled(True)
            
    def validate_and_accept(self):
        """Validate before accepting the dialog."""
        if self.is_new_event:
            selected_data = self.type_combo.currentData()
            if selected_data is None:
                QMessageBox.warning(self, "Invalid Selection", "Please select an event type.")
                return
        
        self.accept()
        
    def get_values(self):
        """Return the edited values as a dictionary."""
        selected_note = self.type_combo.currentData()
        return {
            'note': int(selected_note) if selected_note else self.event_data.get('note'),
            'time': self.time_spin.value(),
            'velocity': self.velocity_spin.value()
        }