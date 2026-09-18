"""
Timeline module for TupperDMX Simulator.

This module provides an advanced timeline widget with waveform visualization
and MIDI event editing.

Deliberately holds no re-exports: `Timeline` lives in `timeline.manager`, whose
clip editor pulls in the stage render engine, and re-exporting it here made
importing ANY timeline submodule — the tracks' drag geometry included — pay for
that. Import the submodule you want.
"""
