import sys
from PySide.QtCore import QObject, Signal, Slot, QThread, QTimer
from PySide.QtGui import QMessageBox

from mozregression.bisector import Bisector, Bisection, NightlyHandler, \
    InboundHandler
from mozregression.download_manager import BuildDownloadManager
from mozregression.test_runner import TestRunner
from mozregression.errors import MozRegressionError

Bisection.EXCEPTION = -1  # new possible value of bisection end


class GuiBuildDownloadManager(QObject, BuildDownloadManager):
    download_progress = Signal(object, int, int)
    download_started = Signal(object)
    download_finished = Signal(object)

    def __init__(self, destdir, **kwargs):
        QObject.__init__(self)
        BuildDownloadManager.__init__(self, None, destdir, **kwargs)

    def _download_started(self, task):
        self.download_started.emit(task)
        BuildDownloadManager._download_started(self, task)

    def _download_finished(self, task):
        self.download_finished.emit(task)
        BuildDownloadManager._download_finished(self, task)

    def focus_download(self, build_info):
        build_url, fname = self._extract_download_info(build_info)
        dest = self.get_dest(fname)
        # first, stop all downloads in background (except the one for this
        # build if any)
        self.cancel(cancel_if=lambda dl: dest != dl.get_dest())

        dl = self.download(build_url, fname)
        if dl:
            dl.set_progress(self.download_progress.emit)
        build_info['build_path'] = dest


class GuiTestRunner(QObject, TestRunner):
    evaluate_started = Signal()
    evaluate_finished = Signal()

    def __init__(self):
        QObject.__init__(self)
        TestRunner.__init__(self)
        self.app_info = {}
        self.verdict = None
        self.launcher = None
        self.launcher_kwargs = {}

    def evaluate(self, build_info, allow_back=False):
        self.launcher = self.create_launcher(build_info)
        self.launcher.start(**self.launcher_kwargs)
        self.app_info = self.launcher.get_app_info()
        self.evaluate_started.emit()

    def finish(self, verdict):
        assert self.launcher
        self.launcher.stop()
        self.verdict = verdict
        self.evaluate_finished.emit()
        self.launcher = None


class GuiBisector(QObject, Bisector):
    started = Signal(object)
    finished = Signal(object, int)
    step_started = Signal(object, int)
    step_build_found = Signal(object, int, object)
    step_finished = Signal(object, int, str)

    def __init__(self, fetch_config, persist=None):
        QObject.__init__(self)
        Bisector.__init__(self, fetch_config, GuiTestRunner(), persist=persist)
        self.download_manager = GuiBuildDownloadManager(self.download_dir)
        self.bisection = None
        self.mid = None
        self.build_infos = None
        self._step_num = 0
        self._bisect_args = None
        self.error = None

        self.download_manager.download_finished.connect(
            self._build_dl_finished)
        self.test_runner.evaluate_finished.connect(self._evaluate_finished)

    def _finish_on_exception(self, bisection):
        self.error = sys.exc_info()
        self.finished.emit(bisection, Bisection.EXCEPTION)

    @Slot()
    def bisect(self):
        # this is a slot so it will be called in the thread
        try:
            Bisector.bisect(self, *self._bisect_args)
        except MozRegressionError:
            self._finish_on_exception(None)

    def _bisect(self, handler, build_data):
        self.bisection = Bisection(handler, build_data,
                                   self.download_manager,
                                   self.test_runner,
                                   self.fetch_config,
                                   dl_in_background=False)
        self._step_num = 0
        # the started signal will be catched by the report model
        # which lives in the main thread. Since here we are on the worker
        # thread, we give some time for qt to deliver the signal before
        # we call _bisect_next.
        self.started.emit(self.bisection)
        QTimer.singleShot(200, self._bisect_next)

    @Slot()
    def _bisect_next(self):
        # this is executed in the working thread
        self._step_num += 1
        self.step_started.emit(self.bisection, self._step_num)
        try:
            self.mid = mid = self.bisection.search_mid_point()
        except MozRegressionError:
            self._finish_on_exception(self.bisection)
            return
        result = self.bisection.init_handler(mid)
        if result != Bisection.RUNNING:
            self.finished.emit(self.bisection, result)
        else:
            self.build_infos = \
                self.bisection.handler.build_infos(mid, self.fetch_config)
            self.download_manager.focus_download(self.build_infos)
            self.step_build_found.emit(self.bisection, self._step_num,
                                       self.build_infos)

    @Slot()
    def _evaluate(self):
        # this is called in the working thread, so installation does not
        # block the ui.
        self.bisection.evaluate(self.build_infos)

    @Slot(object)
    def _build_dl_finished(self, dl):
        # here we are not in the working thread, since the connection was
        # done in the constructor
        if not dl.get_dest() == self.build_infos['build_path']:
            return
        if dl.is_canceled() or dl.error():
            # todo handle this
            return
        # call this in the thread
        QTimer.singleShot(0, self._evaluate)

    @Slot()
    def _evaluate_finished(self):
        # here we are not in the working thread, since the connection was
        # done in the constructor
        self.bisection.update_build_info(self.mid, self.test_runner.app_info)
        self.step_finished.emit(self.bisection, self._step_num,
                                self.test_runner.verdict)
        result = self.bisection.handle_verdict(self.mid,
                                               self.test_runner.verdict)
        if result != Bisection.RUNNING:
            self.finished.emit(self.bisection, result)
        else:
            # call this in the thread
            QTimer.singleShot(0, self._bisect_next)


class BisectRunner(QObject):
    bisector_created = Signal(object)

    def __init__(self, mainwindow):
        QObject.__init__(self)
        self.mainwindow = mainwindow
        self.bisector = None
        self.thread = None

    def bisect(self, fetch_config, options):
        self.stop()
        self.bisector = GuiBisector(fetch_config)
        # create a QThread, and move self.bisector in it. This will
        # allow to the self.bisector slots (connected after the move)
        # to be automatically called in the thread.
        self.thread = QThread()
        self.bisector.moveToThread(self.thread)
        self.bisector.started.connect(self.on_bisection_started)
        self.bisector.download_manager.download_progress.connect(
            self.show_dl_progress)
        self.bisector.test_runner.evaluate_started.connect(
            self.evaluate)
        self.bisector.finished.connect(self.bisection_finished)
        if options['bisect_type'] == 'nightlies':
            handler = NightlyHandler()
            start = options['start_date']
            end = options['end_date']
        else:
            handler = InboundHandler()
            start = options['start_changeset']
            end = options['end_changeset']
        self.thread.start()
        self.bisector._bisect_args = (handler, start, end)
        # this will be called in the worker thread.
        QTimer.singleShot(0, self.bisector.bisect)

    @Slot()
    def stop(self):
        if self.bisector:
            self.bisector.download_manager.cancel()
            self.bisector = None
        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None

    @Slot(object, int, int)
    def show_dl_progress(self, dl, current, total):
        message = "downloading %s: %d/%d" % (dl.get_dest(), current, total)
        self.mainwindow.ui.statusbar.showMessage(message, 2000)

    @Slot()
    def evaluate(self):
        res = QMessageBox.question(self.mainwindow,
                                   "Build Evaluation",
                                   "Is that good ?",
                                   buttons=QMessageBox.Yes | QMessageBox.No)
        verdict = "g"
        if res == QMessageBox.No:
            verdict = "b"
        self.bisector.test_runner.finish(verdict)

    @Slot(object, int)
    def bisection_finished(self, bisection, resultcode):
        if resultcode == Bisection.NO_DATA:
            msg = "Unable to find enough data to bisect."
            dialog = QMessageBox.warning
        elif resultcode == Bisection.EXCEPTION:
            msg = "Error: %s" % self.bisector.error[1]
            dialog = QMessageBox.critical
        else:
            msg = "The bisection is done."
            dialog = QMessageBox.information
        dialog(self.mainwindow, "End of the bisection", msg)
        self.stop()

    @Slot()
    def on_bisection_started(self):
        self.bisector_created.emit(self.bisector)
