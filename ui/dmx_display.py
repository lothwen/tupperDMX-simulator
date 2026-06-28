from typing import Dict
from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtCore import QObject, pyqtSignal

from tupperlogger import get_logger
from config import Config


class DMXDisplayWidget(QTableWidget):
    """Widget for displaying DMX channel values in a table format."""
    
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.logger = get_logger("ui.dmx_display")
        
        # DMX data and mapping
        self.last_dmx_data = bytearray(512)
        self.dmx_channel_to_row_map: Dict[int, int] = {}
        
        # Setup the display
        self._setup_display()
        self._populate_channels()
        
        self.logger.info("DMX display widget initialized")
    
    def _setup_display(self) -> None:
        """Setup the table widget appearance and structure."""

        # Configure table structure
        self.setColumnCount(2)
        self.setHorizontalHeaderLabels(["Channel", "Value"])
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Allow the panel to resize freely; the splitter (and the slide
        # animation) drive the actual width.
        self.setMinimumWidth(0)
        self._distribute_column_widths()

        # Set font
        font = self.font()
        font.setPointSize(10)
        self.setFont(font)

    def _distribute_column_widths(self) -> None:
        """Split the available viewport width 50/50 between the two columns."""
        width = max(0, self.viewport().width())
        if width == 0:
            return
        half = width // 2
        self.setColumnWidth(0, half)
        self.setColumnWidth(1, width - half)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._distribute_column_widths()
    
    def _populate_channels(self) -> None:
        """Populate the table with DMX channels from configuration."""
        used_channels = self.config.get_used_dmx_channels()
        
        # Calculate total rows needed (fixtures + headers + spacers)
        row_count = sum(len(channels) + 2 for channels in used_channels.values())
        self.setRowCount(row_count)
        
        current_row = 0
        
        for fixture_name, channels in used_channels.items():
            # Add fixture header
            self._add_fixture_header(current_row, fixture_name)
            current_row += 1
            
            # Add channel rows
            for channel in channels:
                self._add_channel_row(current_row, channel)
                self.dmx_channel_to_row_map[channel] = current_row
                current_row += 1
            
            # Add spacer row
            self._add_spacer_row(current_row)
            current_row += 1
        
        self.logger.debug(f"Populated {len(used_channels)} fixtures with {len(self.dmx_channel_to_row_map)} channels")
    
    def _add_fixture_header(self, row: int, fixture_name: str) -> None:
        """Add a fixture header row."""
        fixture_item = QTableWidgetItem(fixture_name)
        fixture_item.setBackground(QColor("#404040"))
        fixture_item.setForeground(QColor("white"))
        
        bold_font = QFont()
        bold_font.setBold(True)
        fixture_item.setFont(bold_font)
        
        self.setItem(row, 0, fixture_item)
        self.setSpan(row, 0, 1, 2)
        self.setRowHeight(row, 20)
    
    def _add_channel_row(self, row: int, channel: int) -> None:
        """Add a DMX channel row."""
        # Channel number
        channel_item = QTableWidgetItem(f"{channel:03d}:")
        channel_item.setForeground(QColor("#CCCCCC"))
        self.setItem(row, 0, channel_item)
        
        # Channel value
        value_item = QTableWidgetItem("000")
        value_item.setForeground(QColor("white"))
        self.setItem(row, 1, value_item)
        
        self.setRowHeight(row, 15)
    
    def _add_spacer_row(self, row: int) -> None:
        """Add a spacer row between fixtures."""
        self.setRowHeight(row, 5)
    
    def update_dmx_data(self, dmx_data: bytearray) -> None:
        """Update the display with new DMX data."""
        self.last_dmx_data[:] = dmx_data
        
        # Update displayed values
        for channel, row in self.dmx_channel_to_row_map.items():
            if channel <= len(dmx_data):
                value = dmx_data[channel - 1]  # DMX channels are 1-indexed
                item = self.item(row, 1)
                if item:
                    item.setText(f"{value:03d}")
        
        # Commented because is too verbose
        # self.logger.debug(f"Updated DMX display with {len(dmx_data)} bytes")
    
    def get_channel_value(self, channel: int) -> int:
        """Get the current value for a specific DMX channel."""
        if 1 <= channel <= len(self.last_dmx_data):
            return self.last_dmx_data[channel - 1]
        return 0
    
    def highlight_channel(self, channel: int, highlight: bool = True) -> None:
        """Highlight or unhighlight a specific channel."""
        if channel in self.dmx_channel_to_row_map:
            row = self.dmx_channel_to_row_map[channel]
            for col in [0, 1]:
                item = self.item(row, col)
                if item:
                    if highlight:
                        item.setBackground(QColor("#006600"))
                    else:
                        item.setBackground(QColor("transparent"))
    
    def refresh_configuration(self, config: Config) -> None:
        """Refresh the display when configuration changes."""
        self.config = config
        self.dmx_channel_to_row_map.clear()
        self.clear()
        self._setup_display()
        self._populate_channels()
        self.logger.info("DMX display refreshed with new configuration")