"""
Touch screen driver for 4DSystems GEN4-4DPI-50CT-CLB (EP0510M09) 
https://4dsystems.com.au/gen4-4dpi-50ct-clb 
Uses python3-evdev, https://python-evdev.readthedocs.io/en/latest/index.html

Copyright (c) 2019 Istvan Z. Kovacs
Based on ft5406.py, Copyright (c) 2014 Pimoroni
"""

import glob
import io
import os
import errno
import struct
from collections import namedtuple
import threading
import time
import select
import queue

import asyncio
#import evdev
from evdev import InputDevice, categorize, ecodes

# Event types (checked with evtest)
EV_SYN = 0
EV_KEY = 1
EV_ABS = 3
# Event codes (checked with evtest)
BTN_TOUCH = 330
ABS_X = 0
ABS_Y = 1
ABS_MT_SLOT = 0x2f # 47 MT slot being modified
ABS_MT_POSITION_X = 0x35 # 53 Center X of multi touch position
ABS_MT_POSITION_Y = 0x36 # 54 Center Y of multi touch position
ABS_MT_TRACKING_ID = 0x39 # 57 Unique ID of initiated contact

TOUCH_X = 0
TOUCH_Y = 1

TouchEvent = namedtuple('TouchEvent', ('timestamp', 'type', 'code', 'value'))

# Touch class codes
TS_PRESS = 1
TS_RELEASE = 0
TS_MOVE = 2

class Touch(object):
    def __init__(self, slot, x, y):
        self.slot = slot

        self._x = x
        self._y = y
        self.last_x = -1
        self.last_y = -1

        self._id = -1
        self.events = []
        self.on_move = None
        self.on_press = None
        self.on_release = None
        
    @property
    def position(self):
        return (self.x, self.y)

    @property
    def last_position(self):
        return (self.last_x, self.last_y)

    @property
    def valid(self):
        return self.id > -1

    @property
    def id(self):
        return self._id

    @id.setter
    def id(self, value):
        if value != self._id:
            if value == -1 and not TS_RELEASE in self.events:
                self.events.append(TS_RELEASE)    
            elif not TS_PRESS in self.events:
                self.events.append(TS_PRESS)

        self._id = value

    @property
    def x(self):
        return self._x

    @x.setter
    def x(self, value):
        if value != self._x and not TS_MOVE in self.events:
            self.events.append(TS_MOVE)
        self.last_x = self._x
        self._x = value

    @property
    def y(self):
        return self._y

    @y.setter
    def y(self, value):
        if value != self._y and not TS_MOVE in self.events:
            self.events.append(TS_MOVE)
        self.last_y = self._y
        self._y = value

    def handle_events(self):
        """Run outstanding press/release/move events"""
        for event in self.events:
            if event == TS_MOVE and callable(self.on_move):
                self.on_move(event, self)
            if event == TS_PRESS and callable(self.on_press):
                self.on_press(event, self)
            if event == TS_RELEASE and callable(self.on_release):
                self.on_release(event, self)

        self.events = []


class Touches(list):
    @property
    def valid(self):
        return [touch for touch in self if touch.valid]

class Touchscreen(object):
    """Use udev and evdev to get the touch event"""

    TOUCHSCREEN_EVDEV_NAME = 'EP0510M09'

    def __init__(self, device=None):
        self._device = self.TOUCHSCREEN_EVDEV_NAME if device is None else device
        self._running = False
        
        self._asyncloop = None        
        self._evdev = self._input_device()
        
        self.position = Touch(0, 0, 0)
        self.touches = Touches([Touch(x, 0, 0) for x in range(10)])
#        self._event_queue = queue.Queue()
        self._touch_slot = 0

    def run(self):
        if self._asyncloop is not None:
            return
            
        if self._evdev is not None:
            self._asyncloop = asyncio.get_event_loop()
            self._asyncloop.run_until_complete(self._asyncread(self._evdev))
            self._running = True
        else:
            raise RuntimeError('Touchscreen device: {} was not initialized!'.format(self._device))
        
    def stop(self):
        if self._asyncloop is None:
            return
 
        self._asyncloop.close()
        self._asyncloop = None
        self._running = False
 
    @property
    def _current_touch(self):
        return self.touches[self._touch_slot]

    def close(self):
        self._evdev = None
        self.stop()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()

    def __iter__(self):
        pass
    
    async def _asyncread(self, dev):
        """
        Use asyncio to read touch events
        Handle events after each EV_SYN
        Save events for each EV_ABS
        """
        async for event in dev.async_read_loop():
            
        #    self._event_queue.put(TouchEvent(absevent.event.sec + (absevent.event.usec / 1000000), absevent.event.type, absevent.event.code, absevent.event.value))            
     
        #while not self._event_queue.empty():
        #    event = self._event_queue.get()
        #    self._event_queue.task_done()

            if event.type == ecodes.EV_SYN: # Sync
                for touch in self.touches:
                    touch.handle_events()
                
            if event.type == ecodes.EV_ABS: # Absolute cursor position
                absevent = categorize(event)
                if absevent.event.code == ecodes.ABS_MT_SLOT:
                    self._touch_slot = absevent.event.value
            
                if absevent.event.code == ecodes.ABS_MT_TRACKING_ID: 
                    self._current_touch.id = absevent.event.value
            
                if absevent.event.code == ecodes.ABS_MT_POSITION_X:
                    self._current_touch.x = absevent.event.value
            
                if absevent.event.code == ecodes.ABS_MT_POSITION_Y:
                    self._current_touch.y = absevent.event.value
            
                if absevent.event.code == ecodes.ABS_X:
                    self.position.x = absevent.event.value
            
                if absevent.event.code == ecodes.ABS_Y:
                    self.position.y = absevent.event.value


    def _input_device(self):
        """Returns the evdev device class (not the path to input device!)"""
        for evdev in glob.glob("/sys/class/input/event*"):
            try:
                with io.open(os.path.join(evdev, 'device', 'name'), 'r') as f:
                    if f.read().strip() == self._device:
                        return InputDevice(os.path.join('/dev','input',os.path.basename(evdev)))
            except IOError as e:
                if e.errno != errno.ENOENT:
                    raise
        raise RuntimeError('Unable to locate touchscreen device: {}'.format(self._device))

    def read(self):
        return next(iter(self))


if __name__ == "__main__":
    import signal

    ts = Touchscreen()

    def handle_event(event, touch):
        print(["Release","Press","Move"][event],
            touch.slot,
            touch.x,
            touch.y)

    for touch in ts.touches:
        touch.on_press = handle_event
        touch.on_release = handle_event
        touch.on_move = handle_event

    ts.run()

    try:
        signal.pause()
    except KeyboardInterrupt:
        print("Stopping driver...")
        ts.stop()
        exit()
