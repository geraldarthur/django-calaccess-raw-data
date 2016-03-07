#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
from datetime import datetime
from hurry.filesize import size
from clint.textui import progress
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.template.loader import render_to_string
from django.contrib.humanize.templatetags.humanize import naturaltime
from calaccess_raw.management.commands import CalAccessCommand
from calaccess_raw import (
    get_download_directory,
    get_test_download_directory,
    get_model_list
)
from calaccess_raw.models.tracking import RawDataVersion


class Command(CalAccessCommand):
    help = "Download, unzip, clean and load the latest CAL-ACCESS database ZIP"

    def add_arguments(self, parser):
        """
        Adds custom arguments specific to this command.
        """
        super(Command, self).add_arguments(parser)
        parser.add_argument(
            "--skip-download",
            action="store_false",
            dest="download",
            default=True,
            help="Skip downloading of the ZIP archive"
        )
        parser.add_argument(
            "--skip-clean",
            action="store_false",
            dest="clean",
            default=True,
            help="Skip cleaning up the raw data files"
        )
        parser.add_argument(
            "--skip-load",
            action="store_false",
            dest="load",
            default=True,
            help="Skip loading up the raw data files"
        )
        parser.add_argument(
            "--keep-files",
            action="store_true",
            dest="keep_files",
            default=False,
            help="Keep zip, unzipped, TSV and CSV files"
        )
        parser.add_argument(
            "--noinput",
            action="store_true",
            dest="noinput",
            default=False,
            help="Download the ZIP archive without asking permission"
        )
        parser.add_argument(
            "--test",
            "--use-test-data",
            action="store_true",
            dest="test_data",
            default=False,
            help="Use sampled test data (skips download, clean a load)"
        )
        parser.add_argument(
            "-a",
            "--app-name",
            dest="app_name",
            default="calaccess_raw",
            help="Name of Django app with models into which data will "
                 "be imported (if other not calaccess_raw)"
        )

    def handle(self, *args, **options):
        super(Command, self).handle(*args, **options)

        # set / compute any attributes that multiple class methods need
        self.app_name = options["app_name"]
        self.keep_files = options["keep_files"]
        self.test_mode = options['test_data']
        self.downloading = options['download']
        self.cleaning = options['clean']
        self.loading = options['load']

        if self.test_mode:
            # if using test data, we don't need to download
            self.downloading = False
            # and always keep files when running test data
            self.keep_files = True

        if self.test_mode:
            self.data_dir = get_test_download_directory()
            # need to set this app-wide because cleancalaccessrawfile
            #   also calls get_download_directory
            settings.CALACCESS_DOWNLOAD_DIR = self.data_dir
        else:
            self.data_dir = get_download_directory()

        os.path.exists(self.data_dir) or os.makedirs(self.data_dir)
        self.zip_path = os.path.join(self.data_dir, 'calaccess.zip')
        self.tsv_dir = os.path.join(self.data_dir, "tsv/")

        # Immediately check that the tsv directory exists when using test data,
        #   so we can stop immediately.
        if self.test_mode:
            if not os.path.exists(self.tsv_dir):
                raise CommandError("Data tsv directory does not exist "
                                   "at %s" % self.tsv_dir)
            elif self.verbosity:
                self.log("Using test data")

        self.csv_dir = os.path.join(self.data_dir, "csv/")
        os.path.exists(self.csv_dir) or os.makedirs(self.csv_dir)

        download_metadata = self.get_download_metadata()

        current_release_datetime = download_metadata['last-modified']

        last_started_update = self.get_last_log()
        last_finished_update = self.get_last_log(finished=True)

        try:
            last_download = self.command_logs.filter(
                command='downloadcalaccessrawdata'
            ).order_by('-start_datetime')[0]
        except IndexError:
            last_download = None

        if last_finished_update:
            version_loaded = last_finished_update.version
            since_loaded_version = naturaltime(version_loaded.release_datetime)
        else:
            version_loaded = None
            since_loaded_version = None

        up_to_date = False
        can_resume = False

        if version_loaded:
            if version_loaded.release_datetime == current_release_datetime:
                up_to_date = True
            else:
                # if there's an update started
                if last_started_update:
                    # that didn't finish
                    if not last_started_update.finish_datetime:
                        # can resume it's for the current version
                        if last_started_update.version.release_datetime == current_release_datetime:
                            can_resume = True
                        # can also resume if skipping download
                        elif not self.downloading:
                            can_resume = True
                        # if can't otherwise resume, but there's a previous download
                        elif last_download:
                            # which did finish
                            if last_download.finish_datetime:
                                # can resume as long as the versions
                                # of last download and update are the same
                                if last_download.version == last_started_update.version:
                                    can_resume = True

        if options['noinput']:
            self.resume_mode = can_resume
        else:
            prompt_context = dict(
                current_release_datetime=current_release_datetime,
                expected_size=size(download_metadata['content-length']),
                up_to_date=up_to_date,
                can_resume=can_resume,
                since_loaded_version=since_loaded_version,
            )

            prompt = render_to_string(
                'calaccess_raw/updatecalaccessrawdata.txt',
                prompt_context,
            )

            if can_resume:
                if self.confirm_proceed(prompt):
                    self.resume_mode = True
                else:
                    self.resume_mode = False
                    if not self.confirm_proceed('Do you want re-start your update?\n'):
                        raise CommandError("Update cancelled")
            else:
                self.resume_mode = False
                if not self.confirm_proceed(prompt):
                    raise CommandError("Update cancelled")

        if not self.test_mode:
            if self.resume_mode:
                self.log_record = last_started_update
            else:
                # get or create a version
                # .get_or_create() throws IntegrityError
                try:
                    version = self.raw_data_versions.get(
                        release_datetime=current_release_datetime
                    )
                except RawDataVersion.DoesNotExist:
                    version = self.raw_data_versions.create(
                        release_datetime=current_release_datetime,
                        size=download_metadata['content-length']
                    )
                # create a new log record
                self.log_record = self.command_logs.create(
                    version=version,
                    command=self,
                    called_by=self.get_caller_log()
                )

        # check if download is complete
        if self.resume_mode:
            if self.downloading:
                if last_download.finish_datetime:
                    self.log("Already downloaded")
                    self.downloading = False

        if self.downloading:
            call_command(
                "downloadcalaccessrawdata",
                keep_files=self.keep_files,
                verbosity=self.verbosity,
            )
            if self.verbosity:
                self.duration()

        # execute the other steps that haven't been skipped
        if options['clean']:
            self.clean()
            if self.verbosity:
                self.duration()

        if options['load']:
            self.load()
            if self.verbosity:
                self.duration()

        if self.verbosity:
            self.success("Done!")

        if not self.test_mode:
            self.log_record.finish_datetime = datetime.now()
            self.log_record.save()

    def clean(self):
        """
        Clean up the raw data files from the state so they are
        ready to get loaded into the database.
        """
        if self.verbosity:
            self.header("Cleaning data files")

        tsv_list = os.listdir(self.tsv_dir)

        if self.resume_mode:
            # get finished clean command logs of last update
            prev_cleaned = [
                x.file_name + '.TSV'
                for x in self.log_record.called.filter(
                    command='cleancalaccessrawfile',
                    finish_datetime__isnull=False
                )
            ]
            self.log("{} files already cleaned.".format(len(prev_cleaned)))
            # remove these from tsv_list
            tsv_list = [x for x in tsv_list if x not in prev_cleaned]

        # Loop through all the files in the source directory
        if self.verbosity:
            tsv_list = progress.bar(tsv_list)
        for name in tsv_list:
            call_command(
                "cleancalaccessrawfile",
                name,
                verbosity=self.verbosity,
                keep_files=self.keep_files,
            )

    def load(self):
        """
        Loads the cleaned up csv files into the database
        """
        if self.verbosity:
            self.header("Loading data files")

        model_list = [
            x for x in get_model_list() if os.path.exists(x.objects.get_csv_path())
        ]

        if self.resume_mode:
            # get finished load command logs of last update
            prev_loaded = [
                x.file_name
                for x in self.log_record.called.filter(
                    command='loadcalaccessrawfile',
                    finish_datetime__isnull=False
                )
            ]
            self.log("{} models already loaded.".format(len(prev_loaded)))
            # remove these from model_list
            model_list = [x for x in model_list if x._meta.db_table not in prev_loaded]

        if self.verbosity:
            model_list = progress.bar(model_list)
        for model in model_list:
            call_command(
                "loadcalaccessrawfile",
                model.__name__,
                verbosity=self.verbosity,
                keep_files=self.keep_files,
                app_name=self.app_name,
            )
