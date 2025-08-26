import sys
import mido
import librosa
import numpy as np
import matplotlib.pyplot as plt
import io
import os
from PyQt6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QApplication, QMainWindow, 
    QGraphicsRectItem, QGraphicsLineItem, QGraphicsPixmapItem, 
    QGraphicsTextItem, QGraphicsItem, QWidget, 
    QHBoxLayout, QScrollBar, QSizePolicy, QGraphicsObject,
    QSlider, QPushButton, QGraphicsProxyWidget, QMenu, QDialog,
    QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QDoubleSpinBox,
    QSpinBox, QDialogButtonBox, QMessageBox
)
from PyQt6.QtGui import QColor, QPainter, QBrush, QPen, QPixmap
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPointF, QRectF, QObject
from tupperlogger import get_logger

class CustomTooltip(QLabel):
    """Custom persistent tooltip widget."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("TimelineTooltip")
        self.hide()
        
    def show_at(self, global_pos, text):
        """Show tooltip at global position with given text."""
        self.setText(text)
        self.adjustSize()
        self.move(global_pos)
        self.show()
        self.raise_()
        
    def hide_tooltip(self):
        """Hide the tooltip."""
        self.hide()

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

class DraggableWaveform(QGraphicsObject):
    position_changed = pyqtSignal(QPointF)

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.y_pos = 0

    def boundingRect(self):
        return QRectF(self.pixmap.rect())

    def paint(self, painter, option, widget):
        painter.fillRect(self.boundingRect(), QColor(50, 50, 100, 200))
        painter.drawPixmap(self.boundingRect().toRect(), self.pixmap, self.pixmap.rect())

    def setYPos(self, y):
        self.y_pos = y
        self.setY(y)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            new_pos = QPointF(value)
            new_pos.setY(self.y_pos)
            self.position_changed.emit(new_pos)
            return new_pos
        return super().itemChange(change, value)

class DraggableMidiEvent(QGraphicsObject):
    """A MIDI event box that can be resized by dragging its right border."""
    
    timing_changed = pyqtSignal(int, float)  # event_index, new_time
    edit_requested = pyqtSignal(int)  # event_index
    delete_requested = pyqtSignal(int)  # event_index
    add_at_position_requested = pyqtSignal(float)  # time_seconds
    
    def __init__(self, x, y, width, height, event_index, event_data, parent=None):
        super().__init__(parent)
        # Store only width and height in rect, position with setPos()
        self.rect = QRectF(0, 0, width, height)
        self.setPos(x, y)  # Position the item in scene coordinates
        self.event_index = event_index
        self.event_data = event_data
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True) 
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        
        # Resize handle zone (right edge)
        self.resize_zone_width = 10
        self.is_resizing = False
        self.resize_start_pos = None
        self.original_width = width
        
        # Store reference to timeline for coordinate conversion
        self.timeline = None
        self.text_item = None  # Reference to the associated text label
        
        # Store the last right-click position for "Add event at this position"
        self.last_right_click_scene_pos = None
        
        # Default brush and pen
        self._brush = QBrush(QColor(100, 100, 100))
        self._pen = QPen(QColor(20, 20, 20))
        
        # Tooltip state management
        self._tooltip_shown = False
        self._custom_tooltip = None
        
        # Logger for debug messages
        self.logger = get_logger("ui.timeline.draggable_midi_event")
        
    def boundingRect(self):
        # Return a rect that starts at (0,0) for proper item-local coordinates
        return QRectF(0, 0, self.rect.width(), self.rect.height())
    
    def paint(self, painter, option, widget):
        painter.setBrush(self._brush)
        painter.setPen(self._pen)
        # Paint at (0,0) in item coordinates, not scene coordinates
        painter.drawRect(self.boundingRect())
    
    def setBrush(self, brush):
        self._brush = brush
        self.update()
    
    def setPen(self, pen):
        self._pen = pen
        self.update()
        
    def setRect(self, rect):
        self.prepareGeometryChange()
        self.rect = rect
        self.update()
    
    def set_timeline(self, timeline):
        self.timeline = timeline
    
    def set_text_item(self, text_item):
        """Set reference to the associated text label."""
        self.text_item = text_item
        
    def _show_persistent_tooltip(self, local_pos):
        """Show tooltip with detailed event information."""
        if not self.event_data:
            return
            
        # Create custom tooltip if it doesn't exist
        if not self._custom_tooltip:
            if self.scene() and self.scene().views():
                main_window = self.scene().views()[0].window()
                self._custom_tooltip = CustomTooltip(main_window)
            
        if not self._custom_tooltip:
            return
            
        # Get event information
        event_info = self.event_data.get('info', {})
        part_name = getattr(event_info, 'part', f'Note {self.event_data["note"]}')
        velocity = self.event_data.get('velocity', 0)
        time = self.event_data.get('time', 0.0)
        note = self.event_data.get('note', 0)
        
        # Calculate bar and beat information
        if self.timeline:
            seconds_per_beat = 60.0 / self.timeline.bpm
            beat_number = time / seconds_per_beat
            bar_number = int(beat_number / 4) + 1
            beat_in_bar = int(beat_number % 4) + 1
            
            # Create detailed tooltip text
            tooltip_text = (f"{part_name}\n"
                          f"MIDI Note: {note}\n"
                          f"Time: {time:.3f}s\n"
                          f"Bar: {bar_number}, Beat: {beat_in_bar}\n"
                          f"Velocity: {velocity}")
        else:
            # Fallback if timeline not available
            tooltip_text = (f"{part_name}\n"
                          f"MIDI Note: {note}\n"
                          f"Time: {time:.3f}s\n"
                          f"Velocity: {velocity}")
        
        # Get global position for tooltip
        scene_pos = self.mapToScene(local_pos)
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                global_pos = view.mapToGlobal(view.mapFromScene(scene_pos))
                # Offset tooltip slightly to avoid covering the cursor
                global_pos.setY(global_pos.y() + 20)
                self._custom_tooltip.show_at(global_pos, tooltip_text)
                break
        
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            local_pos = event.pos()
            
            # Check if we're in the resize zone (left edge)
            if local_pos.x() <= self.resize_zone_width:
                self.logger.debug(f"Starting MIDI event resize for event {self.event_index}")
                # Hide tooltip when starting drag operation
                if self._custom_tooltip:
                    self._custom_tooltip.hide_tooltip()
                self._tooltip_shown = False
                
                self.is_resizing = True
                self.resize_start_pos = event.scenePos()
                self.original_x = self.pos().x()
                self.original_width = self.rect.width()
                event.accept()
                return
        elif event.button() == Qt.MouseButton.RightButton:
            # Store the scene position of the right-click for "Add event at this position"
            self.last_right_click_scene_pos = event.scenePos()
            self.show_context_menu(event.screenPos())
            event.accept()
            return
        
        super().mousePressEvent(event)
        
    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self.event_index)
            event.accept()
            return
        
        super().mouseDoubleClickEvent(event)
        
    def show_context_menu(self, global_pos):
        """Show right-click context menu."""
        # Find the main window for proper styling
        main_window = self.timeline.parent() if self.timeline else None
        while main_window and not hasattr(main_window, 'centralWidget'):
            main_window = main_window.parent()
        
        menu = QMenu(main_window)
        
        # Edit Properties action
        edit_action = menu.addAction("Edit Properties...")
        edit_action.triggered.connect(lambda: self.edit_requested.emit(self.event_index))
        
        # Add separator
        menu.addSeparator()
        
        # Add event at this position action
        add_action = menu.addAction("Add event at this position")
        # Calculate time from the mouse click position
        if self.last_right_click_scene_pos and self.timeline:
            click_time = max(0.0, self.last_right_click_scene_pos.x() / self.timeline.PIXELS_PER_SECOND)
            add_action.triggered.connect(lambda: self.add_at_position_requested.emit(click_time))
        else:
            # Fallback to event time if position not available
            event_time = self.event_data['time']
            add_action.triggered.connect(lambda: self.add_at_position_requested.emit(event_time))
        
        # Add separator
        menu.addSeparator()
        
        # Delete Event action
        delete_action = menu.addAction("Delete Event")
        delete_action.triggered.connect(lambda: self.delete_requested.emit(self.event_index))
        
        menu.exec(global_pos)
    
    def mouseMoveEvent(self, event):
        if self.is_resizing and self.resize_start_pos:
            # Calculate new position based on mouse movement (dragging left edge)
            current_pos = event.scenePos()
            delta_x = current_pos.x() - self.resize_start_pos.x()
            new_x = self.original_x + delta_x
            
            # Prevent moving left border before 0.0 seconds (x = 0)
            new_x = max(0, new_x)
            
            # Calculate new width (since we're moving the left edge, width changes inversely)
            right_edge = self.original_x + self.original_width  # Keep right edge fixed
            new_width = max(20, right_edge - new_x)  # Minimum width of 20px
            
            # If new_width would be less than minimum, adjust new_x
            if new_width == 20:
                new_x = right_edge - 20
                # But still ensure we don't go below 0
                new_x = max(0, new_x)
            
            # Update position and size
            self.setPos(new_x, self.pos().y())
            new_rect = QRectF(0, 0, new_width, self.rect.height())
            self.setRect(new_rect)
            
            # Update text position to follow the left edge and recalculate truncation
            if self.text_item and self.timeline:
                self.text_item.setPos(new_x + 5, self.text_item.pos().y())
                
                # Recalculate text truncation based on new width
                # Account for zoom level since text ignores transformations
                zoom_factor = self.timeline.content_view.transform().m11()
                available_width = (new_width - 10) * zoom_factor  # 5px padding on each side, adjusted for zoom
                font_metrics = self.timeline.content_view.fontMetrics()
                full_text = self.text_item.data(0)  # Get the stored full text
                if full_text:
                    text_to_display = self.timeline.get_truncated_text(full_text, available_width, font_metrics)
                    self.text_item.setPlainText(text_to_display)
            
            event.accept()
            return
            
        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event):
        if self.is_resizing and self.timeline:
            # Calculate the new timing for THIS event (the one being moved)
            new_start_time = self.pos().x() / self.timeline.PIXELS_PER_SECOND
            self.logger.debug(f"MIDI event {self.event_index} moved to time {new_start_time:.3f}s")
            self.timing_changed.emit(self.event_index, new_start_time)
            
        self.is_resizing = False
        self.resize_start_pos = None
        super().mouseReleaseEvent(event)
    
    def hoverEnterEvent(self, event):
        # Show tooltip when entering the item
        if self.event_data:
            self._show_persistent_tooltip(event.pos())
            self._tooltip_shown = True
        
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        local_pos = event.pos()
        
        # Change cursor when over resize zone (left edge)
        if local_pos.x() <= self.resize_zone_width:
            # Set cursor on the view instead of the item
            if self.scene() and self.scene().views():
                for view in self.scene().views():
                    view.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            # Set cursor on the view instead of the item
            if self.scene() and self.scene().views():
                for view in self.scene().views():
                    view.setCursor(Qt.CursorShape.ArrowCursor)
            
        super().hoverMoveEvent(event)
        
    def hoverLeaveEvent(self, event):
        # Hide custom tooltip when leaving and reset tooltip state
        if self._custom_tooltip:
            self._custom_tooltip.hide_tooltip()
        self._tooltip_shown = False
        
        # Reset cursor on the view when leaving
        if self.scene() and self.scene().views():
            for view in self.scene().views():
                view.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverLeaveEvent(event)

class ContentView(QGraphicsView):
    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        
        # Enable context menu for right-clicks on empty areas
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        
        # Custom tooltip for piano roll notes
        self._piano_tooltip = None
        self._current_tooltip_item = None
        
    def show_context_menu(self, position):
        """Show context menu for adding events on empty timeline areas."""
        # Convert position to scene coordinates
        scene_pos = self.mapToScene(position)
        
        # Check if we're in the MIDI Events track area
        midi_events_y_start = (self.timeline.RULER_TRACK_HEIGHT + 
                              self.timeline.WAVEFORM_TRACK_HEIGHT + 
                              2 * self.timeline.TRACK_SPACING)
        midi_events_y_end = midi_events_y_start + self.timeline.MIDI_EVENTS_TRACK_HEIGHT
        
        if midi_events_y_start <= scene_pos.y() <= midi_events_y_end:
            # Check if we clicked on an empty area (no MIDI event at this position)
            item_at_pos = self.scene().itemAt(scene_pos, self.transform())
            
            # If there's no item or it's not a DraggableMidiEvent, show add menu
            if not isinstance(item_at_pos, DraggableMidiEvent):
                # Create context menu with main window as parent for proper styling
                main_window = self.timeline.parent()
                while main_window and not hasattr(main_window, 'centralWidget'):
                    main_window = main_window.parent()
                
                menu = QMenu(main_window if main_window else self)
                
                add_action = menu.addAction("Add event at this position")
                
                # Calculate the time from the click position
                time_seconds = max(0.0, scene_pos.x() / self.timeline.PIXELS_PER_SECOND)
                add_action.triggered.connect(lambda: self.timeline.on_add_event_requested(time_seconds))
                
                # Show menu at the clicked position
                menu.exec(self.mapToGlobal(position))

    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            zoom_in_factor = 1.15
            zoom_out_factor = 1 / zoom_in_factor

            if event.angleDelta().y() > 0:
                self.scale(zoom_in_factor, 1)
            else:
                view_width = self.viewport().width()
                scene_width = self.scene().width()
                
                if scene_width <= 0: return

                min_scale = view_width / scene_width
                current_scale = self.transform().m11()

                if current_scale * zoom_out_factor < min_scale:
                    self.resetTransform()
                    self.scale(min_scale, 1)
                else:
                    self.scale(zoom_out_factor, 1)
            self.timeline.update_ruler_labels()
            self.timeline.update_midi_text_truncation()
        else:
            super().wheelEvent(event)

    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        
        # Check for piano roll notes
        current_item = None
        for item in self.timeline.note_items:
            if item.contains(scene_pos):
                current_item = item
                break
        
        # Handle tooltip based on current item
        if current_item:
            # If this is a new item or first time, show tooltip
            if current_item != self._current_tooltip_item:
                # Hide any existing tooltip
                if self._piano_tooltip:
                    self._piano_tooltip.hide_tooltip()
                
                # Create custom tooltip if it doesn't exist
                if not self._piano_tooltip:
                    self._piano_tooltip = CustomTooltip(self.window())
                
                # Show tooltip for this note
                note_info = current_item.data(0)
                note_name = librosa.midi_to_note(note_info['note'])
                seconds_per_beat = 60.0 / self.timeline.bpm
                beat_number = note_info['time'] / seconds_per_beat
                bar_number = int(beat_number / 4) + 1
                beat_in_bar = int(beat_number % 4) + 1
                tooltip_text = f"{note_name}\nTime: {note_info['time']:.2f}s\nBar: {bar_number}, Beat: {beat_in_bar}\nVelocity: {note_info['velocity']}"
                
                global_pos = event.globalPosition().toPoint()
                global_pos.setY(global_pos.y() + 20)  # Offset to avoid cursor
                self._piano_tooltip.show_at(global_pos, tooltip_text)
                self._current_tooltip_item = current_item
        else:
            # No item under cursor, hide tooltip
            if self._piano_tooltip:
                self._piano_tooltip.hide_tooltip()
            self._current_tooltip_item = None
        
        super().mouseMoveEvent(event)

class Timeline(QWidget):
    seek_requested = pyqtSignal(float)
    bpm_detected = pyqtSignal(int)
    waveform_offset_changed = pyqtSignal(float)
    metronome_volume_changed = pyqtSignal(int)
    metronome_mute_toggled = pyqtSignal(bool)
    audio_volume_changed = pyqtSignal(int)
    audio_mute_toggled = pyqtSignal(bool)
    midi_data_changed = pyqtSignal()  # Emitted when MIDI data is modified
    KEY_HEIGHT = 20
    PIANO_WIDTH = 100

    RULER_TRACK_HEIGHT = 30
    WAVEFORM_TRACK_HEIGHT = 100
    MIDI_EVENTS_TRACK_HEIGHT = 50
    
    TRACK_SPACING = 5

    SECONDS_TO_DISPLAY = 240
    PIXELS_PER_SECOND = 20
    
    def __init__(self, midi_manager=None, audio_file=None, initial_metronome_volume=0.5, initial_audio_volume=0.5, parent=None):
        super().__init__(parent)
        self.audio_file = audio_file
        self.midi_manager = midi_manager
        
        self.min_note = 21
        self.max_note = 108
        self.bpm = 120
        
        self.note_items = []
        self.waveform_item = None
        self.midi_event_items = []
        self.piano_roll_items = []
        self.time_grid_items = []
        self.ruler_items = []
        self.ruler_bar_text_items = []
        
        # Cache for current UI data (refreshed from MidiManager)
        self._cached_events = []
        self._cached_notes = []
        
        self._is_panning = False
        self._pan_start_pos = None
        
        # Custom tooltip for piano roll notes
        self._piano_tooltip = None
        self._current_tooltip_item = None
        
        # Logger for debug messages
        self.logger = get_logger("ui.timeline")

        self.metronome_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.metronome_volume_slider.setRange(0, 100)
        self.metronome_volume_slider.setValue(int(initial_metronome_volume * 100))
        self.metronome_volume_slider.setFixedWidth(60)
        self.metronome_volume_slider.valueChanged.connect(self.metronome_volume_changed.emit)

        self.metronome_mute_button = QPushButton("M")
        self.metronome_mute_button.setCheckable(True)
        self.metronome_mute_button.toggled.connect(self.on_mute_toggled)
        self.metronome_mute_button.setFixedSize(20, 20)
        self.metronome_mute_button.setObjectName("MetronomeMuteButton")

        self.metronome_controls_container = QWidget()
        self.metronome_controls_container.setFixedWidth(90)
        self.metronome_controls_container.setObjectName("MetronomeControlsContainer")
        controls_layout = QHBoxLayout(self.metronome_controls_container)
        controls_layout.setContentsMargins(2, 2, 2, 2)
        controls_layout.setSpacing(2)
        controls_layout.addWidget(self.metronome_mute_button)
        controls_layout.addWidget(self.metronome_volume_slider)

        self.audio_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.audio_volume_slider.setRange(0, 100)
        self.audio_volume_slider.setValue(int(initial_audio_volume * 100))
        self.audio_volume_slider.setFixedWidth(60)
        self.audio_volume_slider.valueChanged.connect(self.audio_volume_changed.emit)

        self.audio_mute_button = QPushButton("M")
        self.audio_mute_button.setCheckable(True)
        self.audio_mute_button.toggled.connect(self.on_audio_mute_toggled)
        self.audio_mute_button.setFixedSize(20, 20)
        self.audio_mute_button.setObjectName("AudioMuteButton")

        self.audio_controls_container = QWidget()
        self.audio_controls_container.setFixedWidth(90)
        self.audio_controls_container.setObjectName("AudioControlsContainer")
        audio_controls_layout = QHBoxLayout(self.audio_controls_container)
        audio_controls_layout.setContentsMargins(2, 2, 2, 2)
        audio_controls_layout.setSpacing(2)
        audio_controls_layout.addWidget(self.audio_mute_button)
        audio_controls_layout.addWidget(self.audio_volume_slider)

        self.init_ui()
        self.setup_scenes()

    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header_view = QGraphicsView(self)
        self.header_scene = QGraphicsScene(self)
        self.header_view.setScene(self.header_scene)
        self.header_view.setFixedWidth(self.PIANO_WIDTH)
        self.header_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.header_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.header_view.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding) 
        
        self.content_view = ContentView(self, self)
        self.content_scene = QGraphicsScene(self)
        self.content_view.setScene(self.content_scene)

        layout.addWidget(self.header_view)
        layout.addWidget(self.content_view)

        self.content_view.verticalScrollBar().valueChanged.connect(self.header_view.verticalScrollBar().setValue)
        self.header_view.verticalScrollBar().valueChanged.connect(self.content_view.verticalScrollBar().setValue)

    def setup_scenes(self):
        self.content_width = self.PIXELS_PER_SECOND * self.SECONDS_TO_DISPLAY
        note_range = self.max_note - self.min_note
        self.scene_height = self.RULER_TRACK_HEIGHT + self.WAVEFORM_TRACK_HEIGHT + self.MIDI_EVENTS_TRACK_HEIGHT + (self.KEY_HEIGHT * note_range) + 3 * self.TRACK_SPACING

        self.header_scene.setBackgroundBrush(QColor(30, 30, 30))
        self.header_scene.setSceneRect(0, 0, self.PIANO_WIDTH, self.scene_height)
        
        self.content_scene.setBackgroundBrush(QColor(30, 30, 30))
        self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)

        self.draw_track_headers()
        self.draw_ruler_header()
        self.draw_piano_roll()
        self.draw_playhead()
        self.draw_ruler()
        
        if self.audio_file:
            self.draw_waveform(self.audio_file)
        
        # MIDI data will be loaded via refresh_from_manager()
        self.draw_time_grid()
        
    def refresh_from_manager(self) -> None:
        """Refresh timeline display from MidiManager data."""
        if not self.midi_manager:
            return
            
        # Get current data from MidiManager
        self._cached_events = self.midi_manager.get_events_for_ui()
        self._cached_notes = self.midi_manager.get_notes_for_ui()
        
        # Redraw the UI
        self.redraw_midi_events()
        self.redraw_piano_roll()
        
        # Detect BPM from first event if available
        if self._cached_events:
            # Try to get BPM from MIDI data
            duration = self.midi_manager.get_duration()
            if duration > 0:
                self._detect_bpm_from_data()

    def _detect_bpm_from_data(self) -> None:
        """Detect BPM from MIDI data structure."""
        if not self.midi_manager or not self.midi_manager.midi_data:
            return
            
        # Look for tempo meta messages
        for msg in self.midi_manager.midi_data:
            if msg.is_meta and msg.type == 'set_tempo':
                tempo = mido.tempo2bpm(msg.tempo)
                self.bpm = int(tempo)
                self.bpm_detected.emit(self.bpm)
                return
        
        # Look for velocity-encoded BPM (specific to this application)
        for msg in self.midi_manager.midi_data:
            if not msg.is_meta and msg.type == 'note_on' and msg.note == 40:
                tempo = msg.velocity * 2
                self.bpm = int(tempo)
                self.bpm_detected.emit(self.bpm)
                return

    def on_mute_toggled(self, checked):
        self.metronome_mute_toggled.emit(checked)

    def on_audio_mute_toggled(self, checked):
        self.audio_mute_toggled.emit(checked)

    def draw_ruler_header(self):
        ruler_header_bg = QGraphicsRectItem(0, 0, self.PIANO_WIDTH, self.RULER_TRACK_HEIGHT)
        ruler_header_bg.setBrush(QBrush(QColor(50, 50, 50)))
        ruler_header_bg.setPen(QPen(QColor(20, 20, 20)))
        self.header_scene.addItem(ruler_header_bg)

        proxy = QGraphicsProxyWidget()
        proxy.setWidget(self.metronome_controls_container)
        proxy.setPos(5, 5)
        proxy.setZValue(1)
        self.header_scene.addItem(proxy)

    def draw_playhead(self):
        pen = QPen(QColor(255, 0, 0, 200), 2)
        self.playhead = QGraphicsLineItem(0, 0, 0, self.scene_height)
        self.playhead.setPen(pen)
        self.playhead.setZValue(10)
        self.content_scene.addItem(self.playhead)

    def update_playhead(self, time_seconds):
        x_pos = time_seconds * self.PIXELS_PER_SECOND
        self.playhead.setX(x_pos)

    def draw_waveform(self, audio_file):
        if self.waveform_item:
            self.content_scene.removeItem(self.waveform_item)
            self.waveform_item = None
            
        try:
            y, sr = librosa.load(audio_file, sr=None)
            song_length_seconds = librosa.get_duration(y=y, sr=sr)

            # Extend content width by 5% beyond song duration to show final MIDI events
            extended_duration = song_length_seconds * 1.05
            self.content_width = int(extended_duration * self.PIXELS_PER_SECOND)
            self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)
            self.draw_time_grid()
            self.draw_ruler()

            y_pos = self.RULER_TRACK_HEIGHT + self.TRACK_SPACING
            waveform_height = self.WAVEFORM_TRACK_HEIGHT
            
            dpi = 100
            # Only create waveform for actual song duration, not extended space
            waveform_width = int(song_length_seconds * self.PIXELS_PER_SECOND)
            fig = plt.figure(figsize=(waveform_width / dpi, waveform_height / dpi), dpi=dpi)
            ax = fig.add_subplot(1, 1, 1)
            librosa.display.waveshow(y, sr=sr, ax=ax, color='w')
            ax.set_xlim([0, song_length_seconds])
            ax.axis('off')
            fig.tight_layout(pad=0)
            fig.patch.set_alpha(0)
            ax.patch.set_alpha(0)

            buf = io.BytesIO()
            fig.savefig(buf, format='png', transparent=True)
            buf.seek(0)
            waveform_pixmap = QPixmap()
            waveform_pixmap.loadFromData(buf.read())
            plt.close(fig)

            self.waveform_item = DraggableWaveform(waveform_pixmap)
            self.waveform_item.setYPos(y_pos)
            self.waveform_item.setZValue(0.5)  # Keep waveform above grid but below events
            self.waveform_item.position_changed.connect(self.on_waveform_moved)
            self.content_scene.addItem(self.waveform_item)

        except Exception as e:
            self.logger.error(f"Error drawing waveform: {e}")

    def on_waveform_moved(self, pos):
        offset_sec = pos.x() / self.PIXELS_PER_SECOND
        self.waveform_offset_changed.emit(offset_sec)

    def draw_midi_events_from_manager(self):
        """Draw MIDI events from MidiManager data - kept for backward compatibility."""
        self.redraw_midi_events()

    def set_files(self, audio_file, midi_file=None):
        """Set audio file. MIDI files should be loaded via MidiManager."""
        self.audio_file = audio_file

        # Clear existing items
        for item in self.note_items: self.content_scene.removeItem(item)
        self.note_items.clear()
        if self.waveform_item: self.content_scene.removeItem(self.waveform_item)
        self.waveform_item = None
        for item in self.midi_event_items: self.content_scene.removeItem(item)
        self.midi_event_items.clear()

        # Draw audio waveform
        if audio_file: self.draw_waveform(audio_file)
        
        # MIDI data should be loaded via MidiManager and displayed via refresh_from_manager()
        QTimer.singleShot(0, self.fit_to_width)

    def fit_to_width(self):
        view_width = self.content_view.viewport().width()
        scene_width = self.content_scene.width()
        
        if scene_width <= 0 or view_width <= 0: return

        min_scale = view_width / scene_width
        self.content_view.resetTransform()
        self.content_view.scale(min_scale, 1)
        self.update_ruler_labels()
        self.update_midi_text_truncation()

    def draw_track_headers(self):
        y_offset = self.RULER_TRACK_HEIGHT + self.TRACK_SPACING
        waveform_header_rect = QGraphicsRectItem(0, y_offset, self.PIANO_WIDTH, self.WAVEFORM_TRACK_HEIGHT)
        waveform_header_rect.setBrush(QBrush(QColor(50,50,50)))
        waveform_header_rect.setPen(QPen(QColor(20,20,20)))
        self.header_scene.addItem(waveform_header_rect)
        
        if self.audio_file:
            file_name = os.path.basename(self.audio_file)
            waveform_text = QGraphicsTextItem(file_name)
            waveform_text.setDefaultTextColor(QColor(200,200,200))
            waveform_text.setPos(5, y_offset + 5)
            self.header_scene.addItem(waveform_text)

        proxy = QGraphicsProxyWidget()
        proxy.setWidget(self.audio_controls_container)
        proxy.setPos(5, y_offset + 25)
        self.header_scene.addItem(proxy)

        y_pos = y_offset + self.WAVEFORM_TRACK_HEIGHT + self.TRACK_SPACING
        midi_events_header_rect = QGraphicsRectItem(0, y_pos, self.PIANO_WIDTH, self.MIDI_EVENTS_TRACK_HEIGHT)
        midi_events_header_rect.setBrush(QBrush(QColor(50,50,50)))
        midi_events_header_rect.setPen(QPen(QColor(20,20,20)))
        self.header_scene.addItem(midi_events_header_rect)

        midi_events_text = QGraphicsTextItem("MIDI Events")
        midi_events_text.setDefaultTextColor(QColor(200,200,200))
        midi_events_text.setPos(5, y_pos + 5)
        self.header_scene.addItem(midi_events_text)

    def draw_piano_roll(self):
        for item in self.piano_roll_items: 
            if item.scene() == self.header_scene: self.header_scene.removeItem(item)
            if item.scene() == self.content_scene: self.content_scene.removeItem(item)
        self.piano_roll_items.clear()

        note_range = self.max_note - self.min_note
        if note_range <= 0: return
        
        y_offset = self.RULER_TRACK_HEIGHT + self.WAVEFORM_TRACK_HEIGHT + self.MIDI_EVENTS_TRACK_HEIGHT + 3 * self.TRACK_SPACING
        track_height = self.KEY_HEIGHT * note_range

        piano_rect = QGraphicsRectItem(0, y_offset, self.PIANO_WIDTH, track_height)
        piano_rect.setBrush(QBrush(QColor(10,10,10)))
        piano_rect.setPen(QPen(QColor(20,20,20)))
        self.header_scene.addItem(piano_rect)
        self.piano_roll_items.append(piano_rect)

        for i in range(self.min_note, self.max_note + 1):
            y_pos = y_offset + (self.max_note - i) * self.KEY_HEIGHT
            is_black_key = (i % 12) in [1, 3, 6, 8, 10] 
            
            key_rect = QGraphicsRectItem(0, y_pos, self.PIANO_WIDTH, self.KEY_HEIGHT)
            key_rect.setBrush(QBrush(QColor(100, 100, 100) if is_black_key else QColor(250, 250, 250)))
            key_rect.setPen(QPen(QColor(20, 20, 20)))
            self.header_scene.addItem(key_rect)
            self.piano_roll_items.append(key_rect)

            grid_line = QGraphicsLineItem(0, y_pos, self.content_width, y_pos)
            grid_line.setPen(QPen(QColor(40, 40, 40) if is_black_key else QColor(50, 50, 50)))
            grid_line.setZValue(-1)  # Keep piano roll grid lines in background
            self.content_scene.addItem(grid_line)
            self.piano_roll_items.append(grid_line)

    def draw_ruler(self):
        for item in self.ruler_items: self.content_scene.removeItem(item)
        self.ruler_items.clear()
        self.ruler_bar_text_items.clear()

        ruler_bg = QGraphicsRectItem(0, 0, self.content_width, self.RULER_TRACK_HEIGHT)
        ruler_bg.setBrush(QBrush(QColor(40, 40, 40)))
        ruler_bg.setPen(QPen(Qt.GlobalColor.transparent))
        self.content_scene.addItem(ruler_bg)
        self.ruler_items.append(ruler_bg)

        seconds_per_beat = 60.0 / self.bpm
        seconds_per_bar = seconds_per_beat * 4
        
        if seconds_per_bar == 0: return
        num_bars = int(self.content_scene.width() / (seconds_per_bar * self.PIXELS_PER_SECOND))

        for bar_num in range(num_bars + 1):
            bar_x = bar_num * seconds_per_bar * self.PIXELS_PER_SECOND
            
            bar_tick = QGraphicsLineItem(bar_x, 0, bar_x, self.RULER_TRACK_HEIGHT)
            bar_tick.setPen(QPen(QColor(200, 200, 200)))
            self.content_scene.addItem(bar_tick)
            self.ruler_items.append(bar_tick)

            bar_text = QGraphicsTextItem(str(bar_num + 1))
            bar_text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            bar_text.setDefaultTextColor(QColor(200, 200, 200))
            bar_text.setPos(bar_x + 5, 5)
            self.content_scene.addItem(bar_text)
            self.ruler_items.append(bar_text)
            self.ruler_bar_text_items.append((bar_num + 1, bar_text))

            for beat_num in range(1, 4):
                beat_x = bar_x + (beat_num * seconds_per_beat * self.PIXELS_PER_SECOND)
                beat_tick = QGraphicsLineItem(beat_x, self.RULER_TRACK_HEIGHT * 0.5, beat_x, self.RULER_TRACK_HEIGHT)
                beat_tick.setPen(QPen(QColor(150, 150, 150)))
                self.content_scene.addItem(beat_tick)
                self.ruler_items.append(beat_tick)
        self.update_ruler_labels()

    def set_bpm(self, bpm):
        """Update BPM and redraw ruler and time grid to match new tempo."""
        if self.bpm != bpm:
            self.bpm = bpm
            self.draw_ruler()
            self.draw_time_grid()
            self.logger.info(f"Timeline BPM updated to {bpm}")

    def update_ruler_labels(self):
        transform = self.content_view.transform()
        if not self.ruler_bar_text_items or transform.m11() == 0:
            return

        seconds_per_bar = (60.0 / self.bpm) * 4
        bar_width_pixels = seconds_per_bar * self.PIXELS_PER_SECOND * transform.m11()

        MIN_PIXEL_SPACING = 40

        if bar_width_pixels < MIN_PIXEL_SPACING:
            for bar_num, text_item in self.ruler_bar_text_items:
                if bar_num == 1 or bar_num % 5 == 0:
                    text_item.setVisible(True)
                else:
                    text_item.setVisible(False)
        else:
            for bar_num, text_item in self.ruler_bar_text_items:
                text_item.setVisible(True)

    def update_midi_text_truncation(self):
        """Update text truncation for all MIDI event labels based on current zoom level."""
        if not self._cached_events or not self.midi_event_items:
            return
            
        zoom_factor = self.content_view.transform().m11()
        font_metrics = self.content_view.fontMetrics()
        
        # Find text items in midi_event_items (they alternate with event boxes)
        text_items = [item for item in self.midi_event_items if isinstance(item, QGraphicsTextItem)]
        
        # Update each text item
        for i, text_item in enumerate(text_items):
            if i < len(self._cached_events):
                event_data = self._cached_events[i]
                
                # Calculate box width for this event
                if i < len(self._cached_events) - 1:
                    duration = self._cached_events[i+1]['time'] - event_data['time']
                else:
                    # For last event, use a reasonable default duration
                    song_length = self.midi_manager.get_duration() if self.midi_manager else 240
                    extended_duration = song_length * 1.05
                    duration = extended_duration - event_data['time']
                    if duration <= 0:
                        duration = song_length * 0.05
                
                width = max(duration * self.PIXELS_PER_SECOND, 20)
                available_width = (width - 10) * zoom_factor
                
                # Get full text and truncate appropriately
                full_text = text_item.data(0)
                if full_text:
                    text_to_display = self.get_truncated_text(full_text, available_width, font_metrics)
                    text_item.setPlainText(text_to_display)

    def draw_time_grid(self):
        for item in self.time_grid_items: self.content_scene.removeItem(item)
        self.time_grid_items.clear()

        seconds_per_beat = 60.0 / self.bpm
        total_beats = int(self.content_width / (seconds_per_beat * self.PIXELS_PER_SECOND))
        
        for beat in range(total_beats):
            x_pos = beat * seconds_per_beat * self.PIXELS_PER_SECOND
            line = QGraphicsLineItem(x_pos, self.RULER_TRACK_HEIGHT, x_pos, self.scene_height)
            line.setPen(QPen(QColor(80, 80, 80), 1.5) if beat % 4 == 0 else QPen(QColor(60, 60, 60), 0.8))
            line.setZValue(-1)  # Keep grid lines in background
            self.content_scene.addItem(line)
            self.time_grid_items.append(line)

    def draw_midi_notes(self, midi_file):
        try:
            mid = mido.MidiFile(midi_file)
            notes = []
            active_notes = {}
            current_time = 0
            min_note, max_note = 127, 0

            for msg in mid:
                current_time += msg.time
                if msg.type == 'note_on' and msg.velocity > 0:
                    active_notes[msg.note] = {"time": current_time, "velocity": msg.velocity}
                    min_note = min(min_note, msg.note)
                    max_note = max(max_note, msg.note)
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    if msg.note in active_notes:
                        start_time = active_notes[msg.note]["time"]
                        notes.append({
                            "note": msg.note, "time": start_time,
                            "duration": current_time - start_time,
                            "velocity": active_notes[msg.note]["velocity"]
                        })
                        del active_notes[msg.note]

            # Store notes data for editing
            self.midi_notes_data = notes.copy()

            self.min_note, self.max_note = min_note - 2, max_note + 2
            note_range = self.max_note - self.min_note
            self.scene_height = self.RULER_TRACK_HEIGHT + self.WAVEFORM_TRACK_HEIGHT + self.MIDI_EVENTS_TRACK_HEIGHT + (self.KEY_HEIGHT * note_range) + 3 * self.TRACK_SPACING
            self.content_scene.setSceneRect(0, 0, self.content_width, self.scene_height)
            self.header_scene.setSceneRect(0, 0, self.PIANO_WIDTH, self.scene_height)
            self.draw_piano_roll()

            y_offset = self.RULER_TRACK_HEIGHT + self.WAVEFORM_TRACK_HEIGHT + self.MIDI_EVENTS_TRACK_HEIGHT + 3 * self.TRACK_SPACING
            for note in notes:
                x_pos = note["time"] * self.PIXELS_PER_SECOND
                y_pos = y_offset + (self.max_note - note["note"]) * self.KEY_HEIGHT
                width = note["duration"] * self.PIXELS_PER_SECOND
                note_rect = QGraphicsRectItem(x_pos, y_pos, width, self.KEY_HEIGHT)
                note_rect.setBrush(QBrush(QColor(150, 0, 0, 220)))
                note_rect.setPen(QPen(QColor(250, 250, 250)))
                note_rect.setData(0, note)
                self.content_scene.addItem(note_rect)
                self.note_items.append(note_rect)
        except Exception as e:
            self.logger.error(f"Error loading MIDI file: {e}")

    def on_midi_event_timing_changed(self, event_index, new_start_time):
        """Handle when a MIDI event box left border is dragged, updating this event's start time."""
        if not self.midi_manager or event_index < 0 or event_index >= len(self._cached_events):
            return
        
        # Get the event ID from cached data
        event_data = self._cached_events[event_index]
        event_id = event_data.get('id')
        
        if not event_id:
            self.logger.error(f"No event ID found for event index {event_index}")
            return
        
        # Update via MidiManager
        if self.midi_manager.update_event_timing(event_id, new_start_time):
            # Refresh the timeline display
            self.refresh_from_manager()

    def on_midi_event_edit_requested(self, event_index):
        """Handle edit request for a MIDI event."""
        if not self.midi_manager or event_index < 0 or event_index >= len(self._cached_events):
            return
            
        event_data = self._cached_events[event_index]
        event_id = event_data.get('id')
        
        if not event_id:
            return
            
        # Use MidiManager's note map for the dialog
        dialog = MidiEventEditDialog(event_data, self.midi_manager.midi_note_map)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Get the new values
            new_values = dialog.get_values()
            
            # Update via MidiManager
            if self.midi_manager.edit_event(event_id, **new_values):
                # Refresh the timeline display
                self.refresh_from_manager()
                self.logger.info(f"MIDI event {event_index} edited: note={new_values['note']}, time={new_values['time']:.3f}s, velocity={new_values['velocity']}")

    def on_midi_event_delete_requested(self, event_index):
        """Handle delete request for a MIDI event."""
        if not self.midi_manager or event_index < 0 or event_index >= len(self._cached_events):
            return
            
        event_data = self._cached_events[event_index]
        event_id = event_data.get('id')
        
        if not event_id:
            return
            
        # Confirm deletion
        info = event_data.get('info', None)
        if info and hasattr(info, 'part'):
            event_name = info.part
        else:
            event_name = f"Note {event_data['note']}"
            
        reply = QMessageBox.question(
            None, 
            "Delete MIDI Event", 
            f"Are you sure you want to delete the MIDI event '{event_name}' at {event_data['time']:.2f}s?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # Delete via MidiManager
            if self.midi_manager.delete_event(event_id):
                # Refresh the timeline display
                self.refresh_from_manager()
                self.logger.info(f"MIDI event {event_index} deleted: note={event_data['note']}, time={event_data['time']:.3f}s")

    def on_add_event_requested(self, time_seconds):
        """Handle request to add a new MIDI event at the specified time."""
        if not self.midi_manager:
            return
            
        # Create event data with default values
        new_event_data = {
            'time': time_seconds,
            'velocity': 127,
            'note': None  # Will be set by dialog
        }
        
        dialog = MidiEventEditDialog(new_event_data, self.midi_manager.midi_note_map, is_new_event=True)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Get the new values from dialog
            new_values = dialog.get_values()
            
            # Add via MidiManager
            event_id = self.midi_manager.add_event(
                new_values['note'],
                new_values['time'], 
                new_values['velocity']
            )
            
            if event_id:
                # Refresh the timeline display
                self.refresh_from_manager()
                
                # Get part name for logging
                note_info = self.midi_manager.midi_note_map.get(str(new_values['note']))
                part_name = getattr(note_info, 'part', f'Note {new_values["note"]}') if note_info else f'Note {new_values["note"]}'
                self.logger.info(f"MIDI event added: {part_name} at {new_values['time']:.3f}s, velocity={new_values['velocity']}")
            else:
                self.logger.error(f"Failed to add MIDI event: note {new_values['note']} not in note map")

    def redraw_piano_roll(self):
        """Redraw only the piano roll notes based on cached data."""
        # Remove existing note items
        for item in self.note_items:
            self.content_scene.removeItem(item)
        self.note_items.clear()
        
        # Redraw notes from cached data
        y_offset = self.RULER_TRACK_HEIGHT + self.WAVEFORM_TRACK_HEIGHT + self.MIDI_EVENTS_TRACK_HEIGHT + 3 * self.TRACK_SPACING
        for note in self._cached_notes:
            x_pos = note["time"] * self.PIXELS_PER_SECOND
            y_pos = y_offset + (self.max_note - note["note"]) * self.KEY_HEIGHT
            width = note["duration"] * self.PIXELS_PER_SECOND
            note_rect = QGraphicsRectItem(x_pos, y_pos, width, self.KEY_HEIGHT)
            note_rect.setBrush(QBrush(QColor(150, 0, 0, 220)))
            note_rect.setPen(QPen(QColor(250, 250, 250)))
            note_rect.setData(0, note)
            self.content_scene.addItem(note_rect)
            self.note_items.append(note_rect)

    def redraw_midi_events(self):
        """Redraw the MIDI events track with cached data."""
        # Remove existing MIDI event items
        for item in self.midi_event_items:
            self.content_scene.removeItem(item)
        self.midi_event_items.clear()
        
        if not self._cached_events:
            return
            
        try:
            # Get song length for calculating durations
            song_length_seconds = self.midi_manager.get_duration() if self.midi_manager else 240
            # Use extended timeline duration for last event calculation
            extended_duration = song_length_seconds * 1.05
            y_pos = self.RULER_TRACK_HEIGHT + self.WAVEFORM_TRACK_HEIGHT + 2 * self.TRACK_SPACING

            for i in range(len(self._cached_events)):
                event = self._cached_events[i]
                start_time = event['time']
                if i < len(self._cached_events) - 1:
                    duration = self._cached_events[i+1]['time'] - start_time
                else:
                    # For last event, extend to the end of extended timeline
                    duration = extended_duration - start_time
                    # Ensure minimum width for visibility
                    if duration <= 0:
                        duration = song_length_seconds * 0.05  # Use 5% of song as minimum
                
                x_pos = start_time * self.PIXELS_PER_SECOND
                width = max(duration * self.PIXELS_PER_SECOND, 20)  # Minimum 20px width
                
                # Create draggable MIDI event box
                event_rect = DraggableMidiEvent(x_pos, y_pos, width, self.MIDI_EVENTS_TRACK_HEIGHT, i, event)
                event_rect.set_timeline(self)
                event_rect.timing_changed.connect(self.on_midi_event_timing_changed)
                event_rect.edit_requested.connect(self.on_midi_event_edit_requested)
                event_rect.delete_requested.connect(self.on_midi_event_delete_requested)
                event_rect.add_at_position_requested.connect(self.on_add_event_requested)
                
                info = event['info']
                color = QColor(info.color)
                event_rect.setBrush(QBrush(color))
                event_rect.setPen(QPen(QColor(20,20,20)))
                event_rect.setZValue(1)  # Ensure event box is above other items
                self.content_scene.addItem(event_rect)
                self.midi_event_items.append(event_rect)

                # Determine optimal text color based on background color brightness
                brightness = (color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114)
                text_color = QColor(255, 255, 255) if brightness < 128 else QColor(0, 0, 0)
                
                # Calculate maximum text that fits in the box width
                # Account for zoom level since text ignores transformations
                zoom_factor = self.content_view.transform().m11()
                available_width = (width - 10) * zoom_factor  # 5px padding on each side, adjusted for zoom
                font_metrics = self.content_view.fontMetrics()
                text_to_display = self.get_truncated_text(info.part, available_width, font_metrics)
                
                text_item = QGraphicsTextItem(text_to_display)
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                text_item.setDefaultTextColor(text_color)
                text_item.setPos(x_pos + 5, y_pos + 5)
                text_item.setZValue(2)  # Text should be above the box
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                # Make text item completely transparent to mouse events
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                text_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
                text_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                text_item.setAcceptHoverEvents(False)  # Disable hover events on text
                # Store the full text for tooltip
                text_item.setData(0, info.part)
                
                # Connect the text item to the event box
                event_rect.set_text_item(text_item)
                
                self.content_scene.addItem(text_item)
                self.midi_event_items.append(text_item)

        except Exception as e:
            self.logger.error(f"Error redrawing midi events: {e}")

    def get_modified_midi_data(self):
        """Get the modified MIDI data from MidiManager."""
        if self.midi_manager and self.midi_manager.midi_data:
            return self.midi_manager.midi_data
        return None

    def get_truncated_text(self, text, available_width, font_metrics):
        """Return text truncated to fit within available width, with ellipsis if needed."""
        if available_width <= 0:
            return ""
            
        # If the text fits entirely, return it as-is
        text_width = font_metrics.horizontalAdvance(text)
        if text_width <= available_width:
            return text
            
        # Calculate available width for ellipsis (3 dots)
        ellipsis = "..."
        ellipsis_width = font_metrics.horizontalAdvance(ellipsis)
        available_for_text = available_width - ellipsis_width
        
        if available_for_text <= 0:
            return ellipsis
            
        # Binary search to find the maximum characters that fit
        left, right = 0, len(text)
        best_length = 0
        
        while left <= right:
            mid = (left + right) // 2
            substring = text[:mid]
            substring_width = font_metrics.horizontalAdvance(substring)
            
            if substring_width <= available_for_text:
                best_length = mid
                left = mid + 1
            else:
                right = mid - 1
                
        return text[:best_length] + ellipsis if best_length > 0 else ellipsis

    def get_time_from_pos(self, x_pos):
        scene_x = self.content_view.mapToScene(x_pos, 0).x()
        return scene_x / self.PIXELS_PER_SECOND if self.PIXELS_PER_SECOND > 0 else 0

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            view_pos = self.content_view.mapFrom(self, event.pos())
            if self.content_view.rect().contains(view_pos):
                time_seconds = self.get_time_from_pos(view_pos.x())
                self.update_playhead(time_seconds)
                self.seek_requested.emit(time_seconds)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        view_pos = self.content_view.mapFrom(self, event.pos())
        scene_pos = self.content_view.mapToScene(view_pos)
        
        # Check for piano roll notes
        current_item = None
        for item in self.note_items:
            if item.contains(scene_pos):
                current_item = item
                break
        
        # Handle tooltip based on current item
        if current_item:
            # If this is a new item or first time, show tooltip
            if current_item != self._current_tooltip_item:
                # Hide any existing tooltip
                if self._piano_tooltip:
                    self._piano_tooltip.hide_tooltip()
                
                # Create custom tooltip if it doesn't exist
                if not self._piano_tooltip:
                    self._piano_tooltip = CustomTooltip(self)
                
                # Show tooltip for this note
                note_info = current_item.data(0)
                note_name = librosa.midi_to_note(note_info['note'])
                seconds_per_beat = 60.0 / self.bpm
                beat_number = note_info['time'] / seconds_per_beat
                bar_number = int(beat_number / 4) + 1
                beat_in_bar = int(beat_number % 4) + 1
                tooltip_text = f"{note_name}\nTime: {note_info['time']:.2f}s\nBar: {bar_number}, Beat: {beat_in_bar}\nVelocity: {note_info['velocity']}"
                
                global_pos = event.globalPosition().toPoint()
                global_pos.setY(global_pos.y() + 20)  # Offset to avoid cursor
                self._piano_tooltip.show_at(global_pos, tooltip_text)
                self._current_tooltip_item = current_item
        else:
            # No item under cursor, hide tooltip
            if self._piano_tooltip:
                self._piano_tooltip.hide_tooltip()
            self._current_tooltip_item = None
        
        super().mouseMoveEvent(event)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    script_dir = os.path.dirname(os.path.realpath(__file__))
    mp3_path = os.path.normpath(os.path.join(script_dir, '../../Desktop/TUPPERWARE/Teoría y Práctica/Teoria y Practica [WAVs & MP3s]/198kbps/02-tupperware - La Ciudad de Lona.mp3'))
    midi_path = os.path.normpath(os.path.join(script_dir, '../../Desktop/TUPPERWARE/tupperware-lightning/MIDIs/MIDI-TYP/02-ciudad-de-lona-v0.5.mid'))
    main_win = QMainWindow()
    timeline = Timeline(audio_file=mp3_path, midi_file=midi_path)
    main_win.setCentralWidget(timeline)
    main_win.setGeometry(100, 100, 1200, 800)
    main_win.show()
    sys.exit(app.exec())
