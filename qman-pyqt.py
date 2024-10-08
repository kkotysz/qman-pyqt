#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jan 12 12:05:04 2024
This is the program for Andor CCDOBS queue management. It is written in Python 3.11 and
uses PySide6 (PyQt6) for the GUI. It is based on the PyQt6 designer file qman-pyqt.ui. 
Every change in ui file must be converted to Python file with command:
    pyside6-uic qman-pyqt.ui -o ui_qman_pyqt.py

authors: 
    K. Kotysz:      k.kotysz(at)gmail.com
    P. Mikolajczyk: przeminio(at)gmail.com

    To Add:
    - Information about download status of Finder Chart
    - Functionality: change readout mode in all rows
    - Functionality: move row up/down (grab and drag??)
    - Functionality: 0_CURRENT_QUEUE should resolve object and show it in SkyPlot/FinderChart/SkyView
    - Styling: change color of rows with different image types
    - Styling: change color of cells with different filters


"""

import sys
import os
from PySide6.QtWidgets import QApplication, QMainWindow, QInputDialog, QMessageBox, QVBoxLayout
from widgets import qrow_widget, CurrentQueue, update_table, ObjectInfo, SkyView, SkyPlot, FinderChart
from PySide6.QtCore import SIGNAL, Qt, QTimer, QRunnable, Slot, QThreadPool
from ui_qman_pyqt import Ui_MainWindow
import pandas as pd
import numpy as np
import json
from datetime import datetime as dt
import logging
import fcntl
import argparse
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
import matplotlib.pyplot as plt

# import asyncio
# import time
# import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', filemode='w', filename='qman.log')
# os.environ['QT_MAC_WANTS_LAYER'] = '1'    # to work on MacOS

class Worker(QRunnable):
    '''
    Worker thread

    Inherits from QRunnable to handler worker thread setup, signals and wrap-up.

    :param callback: The function callback to run on this worker thread. Supplied args and
                     kwargs will be passed through to the runner.
    :type callback: function
    :param args: Arguments to pass to the callback function
    :param kwargs: Keywords to pass to the callback function

    '''
    def __init__(self, fn, *args, **kwargs):
        super(Worker, self).__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    @Slot()
    def run(self):
        '''
        Initialise the runner function with passed args, kwargs.
        '''
        self.fn(*self.args, **self.kwargs)

class SingleInstance:
    def __init__(self, lockfile):
        logging.debug(f"SingleInstance: {lockfile} creating")
        self.lockfile = lockfile
        self.fp = None

    def is_running(self):
        try:
            self.fp = open(self.lockfile, 'w')
            fcntl.lockf(self.fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            logging.debug(f"SingleInstance: {self.lockfile} not running")
            return False                                    
        except IOError:
            logging.debug(f"SingleInstance: {self.lockfile} running")
            return True

class QmanMain(QMainWindow):
    def __init__(self, cargs):
        super(QmanMain, self).__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.thread_pool = QThreadPool()  # Initialize QThreadPool
        logging.info(f'QThreadPool maxThreadCount: {self.thread_pool.maxThreadCount()}')
        self.ccdobs = cargs.ccdobs
        self.qobjs = self.get_qlist()
        self.qrows = []
        self.my_obj = ObjectInfo(objname='dummy', objpos=pd.DataFrame(), debug=cargs.debug)
        self.debug = cargs.debug
        if self.debug:
            logging.getLogger().setLevel(logging.DEBUG)
        logging.info(f'QMAN started!')
        logging.info(f'CCDOBS file: {self.ccdobs}')


        # SkyView, SkyPlot, FinderChart instances
        self.skyview = SkyView(self.ui)
        self.skyplot = SkyPlot(self.ui.skyplot, self.my_obj, self.ui)
        self.fchart = FinderChart(self.ui.fchart)
        
        for widget, ui in [(self.skyplot, self.ui.skyplot), (self.fchart, self.ui.fchart)]:
            layout = ui.layout()
            if layout is None:
                layout = QVBoxLayout(ui)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(0)
                layout.setAlignment(Qt.AlignCenter)  # Center alignment for layout
                ui.setLayout(layout)
            layout.addWidget(widget.canvas)
        # Add Matplotlib navigation toolbar for zooming and panning
        self.toolbar = NavigationToolbar(widget.canvas, self, coordinates=False)
        layout.addWidget(self.toolbar)

        # Read objpos.dat file
        self.objpos = pd.DataFrame(columns=['Object', 'RAd', 'RAm', 'RAs', 
                                    'DECd', 'DECm', 'DECs', 'Epoch', 
                                    'Pier side', 'Guiding star', 'Guider position'])
        try:
            self.objpos = pd.read_csv(cargs.objpos, sep='\s+', header=None,
                                    names=['Object', 'RAd', 'RAm', 'RAs', 
                                            'DECd', 'DECm', 'DECs', 'Epoch', 
                                            'Pier side', 'Guiding star', 'Guider position'],
                                    comment='#', skipinitialspace=True)
        except (FileNotFoundError, ValueError) as e:
            logging.warning(f'Warning: No objpos file found!')
            self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Warning: No objpos file found!')

        # Fill QListWidget with objects
        for obj in sorted(self.qobjs['Object'].unique(), key=str.lower):
            self.ui.qobjs.addItem(obj)
        # Connect QListWidget click to function
        self.ui.qobjs.connect(self.ui.qobjs, SIGNAL("itemClicked(QListWidgetItem *)"), self.on_qlist_item_clicked)
        # Set first object as current at startup
        self.ui.qobjs.setCurrentItem(self.ui.qobjs.item(0))
        self.on_qlist_item_clicked(self.ui.qobjs.item(0))
        # self.ui.qview.setMinimumWidth(qrow_widget.sizeHint().width())
        self.ui.qview.setMinimumWidth(380)
        # Connect SetQueue button to function
        self.ui.setq.clicked.connect(self.set_queue)
        self.ui.actionSet_queue.triggered.connect(self.set_queue)
        self.ui.setq.setShortcut('Ctrl+S')
        # Connect Add Queue button to function
        self.ui.actionAdd_queue.triggered.connect(self.add_queue)
        # Connect Add Row button to function
        self.ui.add_row.clicked.connect(self.add_row)
        # Connect Remove Queue button to function
        self.ui.actionRemove_queue.triggered.connect(self.remove_queue)
        # Connect Change Name button to function
        self.ui.actionChange_name.triggered.connect(self.change_name)
        # Connect Resolve button to function
        self.ui.resolve.clicked.connect(self.get_obj_data)
        # Connect Filter inpout to function on text change
        self.ui.qobjs_filter.textChanged.connect(self.filter_qobjs)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.json_dump)
        self.timer.start(1000) 

        self.skyplot_timer = QTimer(self)
        self.skyplot_timer.timeout.connect(self.run_replot_skyplot)
        self.skyplot_timer.start(10000)

        self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Ready!')
        logging.info(f'Ready!')


    @Slot()
    def generate_fchart(self):
        # Create a FinderChartWorker with the user inputs
        worker = Worker(self.fchart.plot, self.my_obj.ra, self.my_obj.dec, self.my_obj.normed_objname)
        # Execute the worker using QThreadPool
        self.thread_pool.start(worker)
        self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Generating finder chart...')
        logging.info(f'Finder chart generated!')
    
    # @Slot()
    # def run_json_dump(self):
    #     worker = Worker(self.json_dump)
    #     self.thread_pool.start(worker)

    # @Slot()
    def run_replot_skyplot(self):
        self.skyplot.my_obj = self.my_obj
        # worker = Worker(self.skyplot.replot)
        # self.thread_pool.start(worker)
        self.skyplot.replot()

    def set_queue(self):
        self.qobjs = self.get_qlist()
        curr_q = CurrentQueue(self.qrows, '0_CURRENT_QUEUE')
        # Set current queue as 0_CURRENT_QUEUE
        self.qobjs = self.qobjs[self.qobjs['Object'] != '0_CURRENT_QUEUE'] # remove all 0_CURRENT_QUEUEs from list
        self.qobjs = pd.concat([curr_q.queue, self.qobjs]) # add current queue to list
        self.save_queue()
        self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Queue set!')
        logging.info(f'Queue set!')

    @update_table
    def json_dump(self):
        rtcoor_path = '/dev/shm/rtcoor.data' if os.path.exists('/dev/shm') else 'rtcoor.data'
        try:
            with open(rtcoor_path, 'w') as f:
                obj_alt, obj_az, obj_ha_sex, ra, dec_sex = self.my_obj.get_info()
                try:
                    json.dump({'ra': np.round(self.my_obj.ra, 5), 
                            'dec': np.round(self.my_obj.dec, 5),
                            'dec_sex': dec_sex.split()[0],
                            'ha': np.round(self.my_obj.ha, 5),
                            'ha_sex': obj_ha_sex,
                            'alt': np.round(self.my_obj.alt, 1), 
                            'az': np.round(self.my_obj.az, 1), 
                            'objname': self.table_data['Object'].values[0]},
                            f)
                except TypeError as e:
                    # logging.error(f'Error: {e}')
                    json.dump({'-': '-', '-': '-', '-': '-', '-': '-', '-': '-', '-': '-', '-': '-'}, f)
                    # self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Error: {e}')
            self.table_data['HA'] = f'{obj_ha_sex}'
            self.table_data['Alt'] = f'{obj_alt}'
        except Exception as e:
            logging.error(f'func: json_dump() Error: {e}')
            self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Error: {e}')
        return self
    
    def save_queue(self):
        # Save all queues to file
        self.current_queue = self.qobjs[self.qobjs['Object'] == '0_CURRENT_QUEUE']
        with open(self.ccdobs, 'w') as f:
            for n, row in self.current_queue.iterrows():
                row['Filter'] = 'Ha narrow' if row['Filter'] == 'Han' else row['Filter']
                row['Filter'] = 'Ha wide' if row['Filter'] == 'Haw' else row['Filter']
                f.write(f"{row['Number']:^5d}{row['Type']:<7s}{row['Filter']:<11s}{row['Exposure']:<7.1f}{row['ROT']:<2s}\n")
            f.write('\n')

            all_queues = self.qobjs[self.qobjs['Object'] != '0_CURRENT_QUEUE']
            for obj in all_queues['Object'].unique():
                obj_queue = all_queues[all_queues['Object'] == obj]
                f.write(f"% {obj}\n")
                for n, row in obj_queue.iterrows():
                    row['Filter'] = 'Ha narrow' if row['Filter'] == 'Han' else row['Filter']
                    row['Filter'] = 'Ha wide' if row['Filter'] == 'Haw' else row['Filter']
                    f.write(f"{row['Number']:^5d}{row['Type']:<7s}{row['Filter']:<11s}{row['Exposure']:<7.1f}{row['ROT']:<2s}\n")
                f.write('\n')
    
    def remove_queue(self):
        # show dialog with name input
        self.qobjs = self.get_qlist()
        name = self.ui.qobjs.currentItem().text()
        self.qobjs = self.qobjs[self.qobjs['Object'] != name]
        self.save_queue()
        self.ui.qobjs.takeItem(self.ui.qobjs.currentRow())
        self.ui.qobjs.setCurrentItem(self.ui.qobjs.item(0))
        self.on_qlist_item_clicked(self.ui.qobjs.item(0))
        self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Queue for {name} removed!')
        logging.info(f'Queue for {name} removed!')

    def add_queue(self):
        # show dialog with name input
        self.qobjs = self.get_qlist()
        text, ok = QInputDialog.getText(self, 'Add queue', 'Object name:')
        if ok:
            self.ui.qobjs.addItem(text)
            toadd = CurrentQueue(self.qrows, text).queue
            self.qobjs = pd.concat([toadd, self.qobjs])
            self.save_queue()
            self.ui.qobjs.sortItems()
            self.ui.qobjs.setCurrentItem(self.ui.qobjs.findItems(text, Qt.MatchExactly)[0])
            self.on_qlist_item_clicked(self.ui.qobjs.currentItem())
            self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Queue for {text} added!')
            logging.info(f'Queue for {text} added!')
    
    def change_name(self):
        # show dialog with name input
        text, ok = QInputDialog.getText(self, 'Change name', 'Object name:')
        if ok:
            old_name = self.ui.qobjs.currentItem().text()
            self.ui.qobjs.currentItem().setText(text)
            self.ui.qobjs.setCurrentItem(self.ui.qobjs.findItems(text, Qt.MatchExactly)[0])
            self.qobjs = self.qobjs.replace(old_name, text)
            self.on_qlist_item_clicked(self.ui.qobjs.currentItem())
            self.save_queue()
            self.ui.qobjs.sortItems()
            self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Name changed to {text}!')
            logging.info(f'Name changed to {text}!')
    
    @update_table
    def add_row(self):
        # Add row to queue
        empty_row = {'Object': 'Foo', 'Number': 1, 'Type': 'Image', 'Filter': 'None', 'Exposure': 1.0, 'ROT': '16'}
        new_qrow = qrow_widget(empty_row, self.qrows, self)
        self.qrows.append(new_qrow)
        self.ui.queue.layout().addWidget(new_qrow)
        qtime = CurrentQueue(self.qrows, 'dummy').countQueue()
        self.table_data['Queue time'] = str(qtime)
        self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Row added!')
        logging.info(f'Row added!')
        return self

    def on_qlist_item_clicked(self, item):
        clicked_queue = self.qobjs[self.qobjs['Object'] == item.text()]
        self.ui.obj_name.setText(item.text())
        for wid in self.qrows:
            wid.deleteLater()
        self.qrows = []
        for n, row in clicked_queue.iterrows():
            new_qrow = qrow_widget(row, self.qrows, self)
            self.qrows.append(new_qrow)
            self.ui.queue.layout().addWidget(new_qrow)
        self.get_obj_data()
        self.generate_fchart()
        self.skyview.create_aladin_view(self.my_obj.c.ra.deg, self.my_obj.c.dec.deg) # create new Aladin view
        self.ui.statusbar.showMessage(f'{dt.now().strftime("%H:%M:%S")} Queue for {item.text()} loaded!')
        logging.info(f'Queue for {item.text()} loaded!')

    def get_qlist(self):
        # Read CCD queue from file
        data = {'Object': [], 'Number': [], 'Type': [], 'Filter': [], 'Exposure': [], 'ROT': []}
        with open(self.ccdobs, 'r') as file:
            current_object_name = '0_CURRENT_QUEUE'
            for line in file:
                line = line.replace('Ha narrow', 'Han') if 'Ha narrow' in line else line
                line = line.replace('Ha wide', 'Haw') if 'Ha wide' in line else line
                line = line.strip()
                if line.startswith('%'):
                    current_object_name = line[1:].strip()
                else:
                    parts = line.split()
                    if parts:
                        data['Object'].append(current_object_name)
                        data['Number'].append(int(parts[0]))
                        data['Type'].append(parts[1])
                        data['Filter'].append(parts[2])
                        data['Exposure'].append(float(parts[3]))
                        data['ROT'].append(str(parts[4]))

        return pd.DataFrame(data)
    
    
    @update_table
    def get_obj_data(self):
        objname = self.ui.obj_name.text()
        qtime = CurrentQueue(self.qrows, objname).countQueue()
        # Initialize table as pandas DataFrame from dictionary
        data = {'Object': [objname], 'RA': [''], 'DEC': [''], 'HA': [''], 'Alt': [''], 'Queue time': [str(qtime)]}
        self.table_data = pd.DataFrame.from_dict(data)
        # Get object data from astropy
        self.my_obj = ObjectInfo(objname=objname, objpos=self.objpos, debug=self.debug)
        self.my_obj.check_objpos() # check if object is in objpos.dat
        obj_alt, obj_az, obj_ha, ra, dec = self.my_obj.get_info()
        self.ui.obj_name.setText(self.my_obj.normed_objname)
        self.table_data['RA'] = f'{ra} (J2000)' if self.my_obj.found else f'{ra} (J2000)*'
        self.table_data['DEC'] = f'{dec} (J2000)' if self.my_obj.found else f'{dec} (J2000)*'
        self.table_data['HA'] = f'{obj_ha}'
        self.table_data['Alt'] = f'{obj_alt}'
        self.skyview.create_aladin_view(self.my_obj.c.ra.deg, self.my_obj.c.dec.deg)
        # if objname != '0_CURRENT_QUEUE' and objname not in self.objpos['Object'].values:
        # elif objname != '0_CURRENT_QUEUE':
        return self

    def filter_qobjs(self):
        filt_obj = self.ui.qobjs_filter.text()
        all_obj = self.qobjs['Object'].unique()
        filtered = filter(lambda x: filt_obj.lower() in x.lower(), all_obj)
        self.ui.qobjs.clear()
        for obj in sorted(filtered, key=str.lower):
            self.ui.qobjs.addItem(obj)

    def main(self):
        lockfile = "/tmp/qman.lock"
        logging.info("Starting QMAN...")

        single_instance_checker = SingleInstance(lockfile)

        if single_instance_checker.is_running():
            logging.info("Another instance is already running, exiting...")
            msg_box = QMessageBox()
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setText("Another instance is already running.")
            msg_box.setWindowTitle("SmartPlugController: INFO")
            msg_box.exec()
            sys.exit(0)

        self.show()


if __name__ == "__main__":
        # print("\n Usage: qman-pyqt.py <ccdobs.lst> <objpos.dat>")
        # print(" Version of 12.01.2024 by K. Kotysz: k.kotysz(at)gmail.com")
        # print("                          P. Mikolajczyk: przeminio(at)gmail.com")
        # parse arguments with argparse
        parser = argparse.ArgumentParser(description='Queue manager for Andor CCD')
        parser.add_argument('ccdobs', type=str, help='CCDOBS file')
        parser.add_argument('--objpos', '-obj', type=str, help='Object position file')
        parser.add_argument('--debug', '-d', action='store_true', help='Debug mode', default=False)
        args = parser.parse_args()
        # print(args)
        app = QApplication(sys.argv)
        window = QmanMain(args).main()
        sys.exit(app.exec())