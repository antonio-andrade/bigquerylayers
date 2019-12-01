# -*- coding: utf-8 -*-
"""
/***************************************************************************
 BigQueryLayersDockWidget
                                 A QGIS plugin
 Add data from BigQuery
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                             -------------------
        begin                : 2018-12-16
        git sha              : $Format:%H$
        copyright            : (C) 2018 by Stefan Mandaric
        email                : stefan.mandaric@unacast.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import os, shutil, subprocess, sys
from queue import Queue

from PyQt5 import QtGui, QtWidgets, uic
from PyQt5.QtCore import pyqtSignal
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsMessageLog, Qgis, QgsTask, QgsApplication, QgsDataSourceUri
from PyQt5.QtCore import QDate, QTime, QDateTime, Qt, pyqtSlot
from qgis.PyQt.QtWidgets import QProgressBar
from qgis.PyQt.QtCore import *

sys.path = [os.path.join(os.path.dirname(__file__), 'libs')] + sys.path
from google.cloud import bigquery

from .background_tasks import BaseQueryTask, RetrieveQueryResultTask, LayerImportTask, ConvertToGeopackage, ExtentsQueryTask

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'bigquery_layers_dockwidget_base.ui'))


class BigQueryLayersDockWidget(QtWidgets.QDockWidget, FORM_CLASS):

    closingPlugin = pyqtSignal()

    def __init__(self, parent=None, iface=None):
        """Constructor."""
        super(BigQueryLayersDockWidget, self).__init__(parent)
        # Set up the user interface from Designer.
        # After setupUI you can access any designer object by doing
        # self.<objectname>, and you can use autoconnect slots - see
        # http://qt-project.org/doc/qt-4.8/designer-using-a-ui-file.html
        # #widgets-and-dialogs-with-auto-connect
        self.setupUi(self)
        self.iface = iface

        self.client = None
        self.base_query_job = Queue()
        #self.base_query_result = Queue()
        self.result_queue = Queue()
        self.file_queue = Queue()
        self.converted_file_queue = Queue()
        self.extent_query_job = Queue()

        # Elements associated with base query
        self.base_query_elements = [self.project_edit, self.query_edit, self.run_query_button]

        # Elements associated with layer imports
        self.layer_import_elements = [self.geometry_column_combo_box, self.add_all_button,
                                      self.add_extents_button, self.geometry_column_label]
        for elm in self.layer_import_elements:
            elm.setEnabled(False)

        # Handle button clicks
        self.run_query_button.clicked.connect(self.run_base_query_handler)
        self.add_all_button.clicked.connect(self.add_layer_button_handler)
        self.add_extents_button.clicked.connect(self.add_layer_button_handler)

        # Changed text
        self.project_edit.textChanged.connect(self.text_changed_handler)
        self.query_edit.textChanged.connect(self.text_changed_handler)

        self.base_query_complete = False

    def closeEvent(self, event):
        self.closingPlugin.emit()
        event.accept()

    def text_changed_handler(self):
        self.base_query_complete = False
        self.query_progress_field.clear()
        self.geometry_column_combo_box.clear()

        for elm in self.layer_import_elements:
            elm.setEnabled(False)

    def run_base_query_handler(self):
        QgsMessageLog.logMessage('Running base query', 'BigQuery Layers', Qgis.Info)
        project_name = self.project_edit.text()
        query = self.query_edit.toPlainText()
        self.client = bigquery.Client(project_name)

        self.base_query_job = Queue()
        self.base_query_job.put(self.client.query(query))


        for elm in self.base_query_elements + self.layer_import_elements:
            elm.setEnabled(False)
        self.run_query_button.setText('Running...')
        self.run_query_button.repaint()
        
        self.base_query_task = BaseQueryTask('Background Query',
            self.iface,
            self.base_query_job,
            self.query_progress_field,
            self.geometry_column_combo_box,
            self.base_query_elements,
            self.layer_import_elements,
            self.run_query_button
            )
        QgsApplication.taskManager().addTask(self.base_query_task)

        QgsMessageLog.logMessage('After task manager', 'BigQuery Layers', Qgis.Info)
    
    def add_layer_button_handler(self):
        geom_field = self.geometry_column_combo_box.currentText()

        geom_column = self.geometry_column_combo_box.currentText()

        elements_in_layer = Queue()
        
        upstream_taks_canceled = Queue()
        upstream_taks_canceled.put(False)

        self.file_queue = Queue()
        self.extent_query_job = Queue()

        for elm in self.base_query_elements + self.layer_import_elements:
            elm.setEnabled(False)
        
        if self.sender().objectName() == 'add_all_button':
            QgsMessageLog.logMessage('Pressed add all', 'BigQuery Layers', Qgis.Info)
            self.add_all_button.setText('Adding layer...')

            self.parent_task = LayerImportTask('BigQuery layer import', self.iface, self.file_queue, self.add_all_button, self.add_extents_button, self.base_query_elements, self.layer_import_elements, elements_in_layer, upstream_taks_canceled, geom_column)

            # TASK 1: DOWNLOAD
            self.download_task = RetrieveQueryResultTask('Retrieve query result', self.iface, self.base_query_job, self.file_queue, elements_in_layer, upstream_taks_canceled)

            # TASK 2: Convert
            self.convert_task = ConvertToGeopackage('Convert to Geopackage', self.iface, geom_column, self.file_queue, upstream_taks_canceled)
            
            self.parent_task.addSubTask(self.download_task, [], QgsTask.ParentDependsOnSubTask)
            self.parent_task.addSubTask(self.convert_task, [self.download_task], QgsTask.ParentDependsOnSubTask)
            
            QgsApplication.taskManager().addTask(self.parent_task)

        elif self.sender().objectName() == 'add_extents_button':
            self.add_extents_button.setText('Adding layer...')

            extent = self.iface.mapCanvas().extent()

            # Reproject extents if project CRS is not EPSG:4326
            project_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
            
            if project_crs != QgsCoordinateReferenceSystem(4326):
                crcTarget = QgsCoordinateReferenceSystem(4326)
                transform = QgsCoordinateTransform(project_crs, crcTarget, QgsProject.instance())
                extent = transform.transform(extent)

            self.parent_task = LayerImportTask('BigQuery layer import', self.iface, self.file_queue, self.add_all_button, self.add_extents_button, self.base_query_elements, self.layer_import_elements, elements_in_layer, upstream_taks_canceled, geom_column)

            # TASK 1: Extents query
            self.extents_query_task = ExtentsQueryTask('Select window extents', self.iface, self.client,
            self.base_query_job, self.extent_query_job, extent.asWktPolygon(), geom_column, upstream_taks_canceled)

            # TASK 2: Retrive - from extent querty
            self.download_task = RetrieveQueryResultTask('Retrieve query result', self.iface, self.extent_query_job, self.file_queue, elements_in_layer, upstream_taks_canceled)

            # TASK 3: Convert
            self.convert_task = ConvertToGeopackage('Convert to Geopackage', self.iface, geom_column, self.file_queue, upstream_taks_canceled)

            self.parent_task.addSubTask(self.extents_query_task, [], QgsTask.ParentDependsOnSubTask)
            self.parent_task.addSubTask(self.download_task, [self.extents_query_task], QgsTask.ParentDependsOnSubTask)
            self.parent_task.addSubTask(self.convert_task, [self.extents_query_task, self.download_task], QgsTask.ParentDependsOnSubTask)
            

            QgsApplication.taskManager().addTask(self.parent_task)

