import os,sys,shutil
import datetime
import configparser
from plumbum import cli
from plumbum import colors

from utils import covert_config_to_dic, colorstr
try:
    import gdal
except:
    from osgeo import gdal

gdal.PushErrorHandler('CPLQuietErrorHandler')

prefix = colorstr('red', 'bold', 'CONFIG DOES NOT EXIST:')

class App(cli.Application):
    PROGNAME = colors.green
    VERSION = colors.blue

    @cli.switch(["-c"], str, mandatory=True, help="a .ini file describing the data to be downloaded")
    def config_file(self, config_f):
        self._config_f = config_f
        if not os.path.exists(config_f):
            print(f"{prefix}: {config_f}")
            sys.exit(-1)
        config = configparser.ConfigParser()
        config.read(config_f)
        config_dic = covert_config_to_dic(config)
        self._config_dic = config_dic


    def main(self, *args):
        from gee_downloader import GEEDownloader
        downloader = GEEDownloader(**self._config_dic)

        ext = os.path.splitext(self._config_f)[-1]
        now = datetime.datetime.now()

        shutil.copy(self._config_f, os.path.join(downloader.save_dir,
                                                 os.path.basename(self._config_f).
                                                 replace(ext,
                                                         f'_{now.year}{str.zfill(str(now.month), 2)}'
                                                         f'{str.zfill(str(now.day),2)}-{str.zfill(str(now.hour),2)}'
                                                         f'{str.zfill(str(now.minute),2)}{ext}')))
        downloader.run()


if __name__ == '__main__':
    App.run()



