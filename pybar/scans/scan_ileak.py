import logging
import numpy as np
import tables as tb
import progressbar

from pybar.fei4.register_utils import make_box_pixel_mask_from_col_row
from pybar.fei4_run_base import Fei4RunBase
from pybar.run_manager import RunManager
from pybar.analysis.plotting.plotting import plot_three_way


class IleakScan(Fei4RunBase):
    '''Analog scan
    '''
    _default_run_conf = {
        "pixels": (np.dstack(np.where(make_box_pixel_mask_from_col_row([1, 16], [1, 36]) == 1)) + 1).tolist()[0],  # list of (col, row) tupels. From 1 to 80/336.
        "mask_steps": 1,  # mask steps, be carefull PlsrDAC injects different charge for different mask steps
        "n_injections": 100,  # number of injections
        "scan_parameters": [('PlsrDAC', 280)],  # the PlsrDAC setting
        "use_enable_mask": False,  # if True, use Enable mask during scan, if False, all pixels will be enabled
        "enable_shift_masks": ["Enable", "C_High", "C_Low"],  # enable masks shifted during scan
        "disable_shift_masks": [],  # disable masks shifted during scan
        "pulser_dac_correction": False,  # PlsrDAC correction for each double column
        "enable_tdc": False,  # if True, enables TDC (use RX2)
        "same_mask_for_all_dc": True,  # if True, all columns have the same mask, if False, mask will be enabled only where injected
        "enable_double_columns": None,  # List of double columns which will be enabled during scan. None will select all double columns
        "enable_mask_steps": None,  # List of mask steps which will be applied. None will select all mask steps.
    }

    def configure(self):
        self.dut['Multimeter'].init()
        logging.info('Initialized multimeter %s' % self.dut['Multimeter'].get_name())
        commands = []
        commands.extend(self.register.get_commands("ConfMode"))
        self.register.set_pixel_register_value('Imon', 0)
        commands.extend(self.register.get_commands("WrFrontEnd", same_mask_for_all_dc=True, name='Imon'))
        self.register_utils.send_commands(commands)
        self.ileakmap = np.zeros(shape=(80, 336))

    def scan(self):
        logging.info("Scanning %d pixels" % len(self.pixels))
        progress_bar = progressbar.ProgressBar(widgets=['', progressbar.Percentage(), ' ', progressbar.Bar(marker='*', left='|', right='|'), ' ', progressbar.AdaptiveETA()], maxval=len(self.pixels), term_width=80)
        progress_bar.start()
        
        data_out = self.raw_data_file.h5_file.createCArray(self.raw_data_file.h5_file.root, name='Ileak_map', title='Leakage current per pixel in arbitrary units', atom=tb.Atom.from_dtype(self.ileakmap.dtype), shape=self.ileakmap.shape, filters=tb.Filters(complib='blosc', complevel=5, fletcher32=False))

        for pixel_index, (column, row) in enumerate(self.pixels):
            # Set Imon for actual pixel and configure FE
            mask = np.zeros(shape=(80, 336))
            mask[column - 1, row - 1] = 1
            commands = []
            commands.extend(self.register.get_commands("ConfMode"))
            self.register.set_pixel_register_value('Imon', mask)
            commands.extend(self.register.get_commands("WrFrontEnd", same_mask_for_all_dc=False, name='Imon'))
            self.register_utils.send_commands(commands)
            # Read and store voltage
            voltage_string = self.dut['Multimeter'].get_voltage()
            voltage = float(voltage_string.split(',')[0][:-4])
            self.ileakmap[column - 1, row - 1] = voltage

            progress_bar.update(pixel_index)
        
        progress_bar.finish()
        
        data_out[:] = self.ileakmap

    def analyze(self):
        with tb.open_file(self.output_filename + '.h5', 'r') as in_file_h5:
            data = in_file_h5.root.Ileak_map[:]
            data = np.ma.masked_where(data == 0, data)
            plot_three_way(hist=data.transpose(), title="Ileak", x_axis_title="Ileak", filename=self.output_filename + '.pdf')#, minimum=0, maximum=np.amax(data))


if __name__ == "__main__":
    RunManager('../configuration.yaml').run_run(IleakScan)
