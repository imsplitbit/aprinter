from __future__ import print_function
import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '../common'))
import argparse
import json
import re
import string
import config_common
import config_reader

IDENTIFIER_REGEX = '\\A[A-Za-z][A-Za-z0-9_]{0,127}\\Z'

class GenState(object):
    def __init__ (self):
        self._subst = {}
        self._config_options = []
        self._constants = []
        self._platform_includes = []
        self._aprinter_includes = set()
        self._objects = {}
        self._singleton_objects = {}
        self._finalize_actions = []
        self._global_code = []
        self._init_calls = []
    
    def add_subst (self, key, val, indent=-1):
        self._subst[key] = {'val':val, 'indent':indent}
    
    def add_config (self, name, dtype, value):
        self._config_options.append({'name':name, 'dtype':dtype, 'value':value})
        return name
    
    def add_float_config (self, name, value):
        return self.add_config(name, 'DOUBLE', '{:.17E}'.format(value))
    
    def add_bool_config (self, name, value):
        return self.add_config(name, 'BOOL', 'true' if value else 'false')
    
    def add_float_constant (self, name, value):
        self._constants.append({'type':'using', 'name':name, 'value':'AMBRO_WRAP_DOUBLE({:.17E})'.format(value)})
        return name
    
    def add_int_constant (self, dtype, name, value):
        m = re.match('\\A(u?)int(8|16|32|64)\\Z', dtype)
        assert m
        u = m.group(1)
        b = m.group(2)
        self._constants.append({'type':'static {}_t const'.format(dtype), 'name':name, 'value':'{}INT{}_C({})'.format(u.upper(), b, value)})
        return name
    
    def add_platform_include (self, inc_file):
        self._platform_includes.append(inc_file)
    
    def add_aprinter_include (self, inc_file):
        self._aprinter_includes.add(inc_file)
    
    def register_objects (self, kind, config, key):
        if kind not in self._objects:
            self._objects[kind] = {}
        for obj_config in config.iter_list_config(key, max_count=20):
            name = obj_config.get_string('Name')
            if name in self._objects[kind]:
                obj_config.path().error('Duplicate {} name'.format(kind))
            self._objects[kind][name] = obj_config
    
    def get_object (self, kind, config, key):
        name = config.get_string(key)
        if kind not in self._objects or name not in self._objects[kind]:
            config.key_path(key).error('Nonexistent {} specified'.format(kind))
        return self._objects[kind][name]
    
    def register_singleton_object (self, kind, value):
        assert kind not in self._singleton_objects
        self._singleton_objects[kind] = value
        return value
    
    def get_singleton_object (self, kind):
        assert kind in self._singleton_objects
        return self._singleton_objects[kind]
    
    def add_global_code (self, priority, code):
        self._global_code.append({'priority':priority, 'code':code})
    
    def add_isr (self, isr):
        self.add_global_code(-1, isr)
    
    def add_init_call (self, priority, init_call):
        self._init_calls.append({'priority':priority, 'init_call':init_call})
    
    def add_finalize_action (self, action):
        self._finalize_actions.append(action)
    
    def finalize (self):
        for action in reversed(self._finalize_actions):
            action()
        
        for so in self._singleton_objects.itervalues():
            if hasattr(so, 'finalize'):
                so.finalize()
        
        self.add_subst('GENERATED_WARNING', 'WARNING: This file was automatically generated!')
        self.add_subst('EXTRA_CONSTANTS', ''.join('{} {} = {};\n'.format(c['type'], c['name'], c['value']) for c in self._constants))
        self.add_subst('ConfigOptions', ''.join('APRINTER_CONFIG_OPTION_{}({}, {}, ConfigNoProperties)\n'.format(c['dtype'], c['name'], c['value']) for c in self._config_options))
        self.add_subst('PLATFORM_INCLUDES', ''.join('#include <{}>\n'.format(inc) for inc in self._platform_includes))
        self.add_subst('AprinterIncludes', ''.join('#include <aprinter/{}>\n'.format(inc) for inc in sorted(self._aprinter_includes)))
        self.add_subst('GlobalCode', ''.join('{}\n'.format(gc['code']) for gc in sorted(self._global_code, key=lambda x: x['priority'])))
        self.add_subst('InitCalls', ''.join('    {}\n'.format(ic['init_call']) for ic in sorted(self._init_calls, key=lambda x: x['priority'])))
    
    def get_subst (self):
        res = {}
        for (key, subst) in self._subst.iteritems():
            val = subst['val']
            indent = subst['indent']
            res[key] = val if type(val) is str else val.build(indent)
        return res

class GenConfigReader(config_reader.ConfigReader):
    def get_int_constant (self, key):
        return str(self.get_int(key))
    
    def get_bool_constant (self, key):
        return 'true' if self.get_bool(key) else 'false'

    def get_float_constant (self, key):
        return '{:.17E}'.format(self.get_float(key))

    def get_identifier (self, key, validate=None):
        val = self.get_string(key)
        if not re.match(IDENTIFIER_REGEX, val):
            self.key_path(key).error('Incorrect format.')
        if validate is not None and not validate(val):
            self.key_path(key).error('Custom validation failed.')
        return val
    
    def get_id_char (self, key):
        val = self.get_string(key)
        if val not in string.ascii_uppercase:
            self.key_path(key).error('Incorrect format.')
        return val
    
    def do_selection (self, key, sel_def):
        for config in self.enter_config(key):
            try:
                result = sel_def.run(config.get_string('_compoundName'), config)
            except config_common.SelectionError:
                config.path().error('Unknown choice.')
            return result
    
    def do_list (self, key, elem_cb, max_count):
        elems = []
        for (i, config) in enumerate(self.iter_list_config(key, max_count=max_count)):
            elems.append(elem_cb(config, i))
        return TemplateList(elems)

class TemplateExpr(object):
    def __init__ (self, name, args):
        self._name = name
        self._args = args
    
    def build (self, indent):
        if indent == -1:
            initiator = ''
            separator = ', '
            terminator = ''
            child_indent = -1
        else:
            initiator = '\n' + '    ' * (indent + 1)
            separator = ',' + initiator
            terminator = '\n' + '    ' * indent
            child_indent = indent + 1
        return '{}<{}{}{}>'.format(self._name, initiator, separator.join(_build_template_arg(arg, child_indent) for arg in self._args), terminator)

def _build_template_arg (arg, indent):
    if type(arg) is str or type(arg) is int or type(arg) is long:
        return str(arg)
    if type(arg) is bool:
        return 'true' if arg else 'false'
    return arg.build(indent)

class TemplateList(TemplateExpr):
    def __init__ (self, args):
        TemplateExpr.__init__(self, 'MakeTypeList', args)

class TemplateChar(object):
    def __init__ (self, ch):
        self._ch = ch
    
    def build (self, indent):
        return '\'{}\''.format(self._ch)

def setup_platform(gen, config, key):
    platform_sel = config_common.Selection()
    
    @platform_sel.option('At91Sam3x8e')
    def option(platform):
        gen.add_platform_include('aprinter/platform/at91sam3x/at91sam3x_support.h')
        gen.add_init_call(-1, 'platform_init();')
    
    @platform_sel.option('Teensy3')
    def option(platform):
        gen.add_platform_include('aprinter/platform/teensy3/teensy3_support.h')
    
    @platform_sel.options(['AVR ATmega2560', 'AVR ATmega1284p'])
    def option(platform_name, platform):
        gen.add_platform_include('avr/io.h')
        gen.add_platform_include('aprinter/platform/avr/avr_support.h')
        gen.add_init_call(-3, 'sei();')
    
    config.do_selection(key, platform_sel)

class CommonClock(object):
    def __init__ (self, gen, config, clockdef_func):
        self._gen = gen
        self._clockdef = config_common.FunctionDefinedClass(clockdef_func)
        gen.add_aprinter_include(self._clockdef.INCLUDE)
        gen.add_subst('CLOCK_CONFIG', self._clockdef.CLOCK_CONFIG(config))
        gen.add_subst('CLOCK', self._clockdef.CLOCK_EXPR)
        self._interrupt_timers = []
        
        primary_timer_str = config.get_string('primary_timer')
        match = re.match(self._clockdef.TIMER_RE, primary_timer_str) 
        if not match:
            config.key_path('primary_timer').error('Incorrect value.')
        self._primary_timer = match.group(1)
    
    def add_interrupt_timer (self, name, user, clearance, path):
        m = re.match(self._clockdef.CHANNEL_RE, name)
        if m is None:
            path.error('Incorrect OC unit format.')
        it = {'tc':m.group(1), 'channel':m.group(2)}
        self._interrupt_timers.append(it)
        clearance_extra = ', {}'.format(clearance) if clearance is not None else ''
        self._gen.add_isr(self._clockdef.INTERRUPT_TIMER_ISR(it, user))
        return self._clockdef.INTERRUPT_TIMER_EXPR(it, clearance_extra)
    
    def finalize (self):
        clock = {'primary_timer': self._primary_timer}
        if hasattr(self._clockdef, 'CLOCK_ISR'):
            self._gen.add_isr(self._clockdef.CLOCK_ISR(clock))
        tcs = set(it['tc'] for it in self._interrupt_timers)
        tcs.remove(self._primary_timer)
        tcs = [self._primary_timer] + sorted(tcs)
        self._gen.add_subst('CLOCK_TCS', 'using ClockTcsList = MakeTypeList<{}>;'.format(', '.join(self._clockdef.TIMER_EXPR(tc) for tc in tcs)))
        if hasattr(self._clockdef, 'TIMER_ISR'):
            for tc in tcs:
                self._gen.add_isr(self._clockdef.TIMER_ISR(tc))

def At91Sam3xClockDef(x):
    x.INCLUDE = 'system/At91Sam3xClock.h'
    x.CLOCK_EXPR = 'At91Sam3xClock<MyContext, Program, clock_timer_prescaler, ClockTcsList>'
    x.TIMER_RE = '\\ATC([0-9])\\Z'
    x.CHANNEL_RE = '\\ATC([0-9])([A-C])\\Z'
    x.CLOCK_CONFIG = lambda config: 'static int const clock_timer_prescaler = {};'.format(config.get_int_constant('prescaler'))
    x.INTERRUPT_TIMER_EXPR = lambda it, clearance_extra: 'At91Sam3xClockInterruptTimerService<At91Sam3xClockTC{}, At91Sam3xClockComp{}{}>'.format(it['tc'], it['channel'], clearance_extra)
    x.INTERRUPT_TIMER_ISR = lambda it, user: 'AMBRO_AT91SAM3X_CLOCK_INTERRUPT_TIMER_GLOBAL(At91Sam3xClockTC{}, At91Sam3xClockComp{}, {}, MyContext())'.format(it['tc'], it['channel'], user)
    x.TIMER_EXPR = lambda tc: 'At91Sam3xClockTC{}'.format(tc)
    x.TIMER_ISR = lambda tc: 'AMBRO_AT91SAM3X_CLOCK_TC{}_GLOBAL(MyClock, MyContext())'.format(tc)

def Mk20ClockDef(x):
    x.INCLUDE = 'system/Mk20Clock.h'
    x.CLOCK_EXPR = 'Mk20Clock<MyContext, Program, clock_timer_prescaler, ClockTcsList>'
    x.TIMER_RE = '\\AFTM([0-9])\\Z'
    x.CHANNEL_RE = '\\AFTM([0-9])_([0-9])\\Z'
    x.CLOCK_CONFIG = lambda config: 'static int const clock_timer_prescaler = {};'.format(config.get_int_constant('prescaler'))
    x.INTERRUPT_TIMER_EXPR = lambda it, clearance_extra: 'Mk20ClockInterruptTimerService<Mk20ClockFTM{}, {}{}>'.format(it['tc'], it['channel'], clearance_extra)
    x.INTERRUPT_TIMER_ISR = lambda it, user: 'AMBRO_MK20_CLOCK_INTERRUPT_TIMER_GLOBAL(Mk20ClockFTM{}, {}, {}, MyContext())'.format(it['tc'], it['channel'], user)
    x.TIMER_EXPR = lambda tc: 'Mk20ClockFtmSpec<Mk20ClockFTM{}>'.format(tc)
    x.TIMER_ISR = lambda tc: 'AMBRO_MK20_CLOCK_FTM_GLOBAL({}, MyClock, MyContext())'.format(tc)

def AvrClockDef(x):
    x.INCLUDE = 'system/AvrClock.h'
    x.CLOCK_EXPR = 'AvrClock<MyContext, Program, ClockPrescaleDivide, ClockTcsList>'
    x.TIMER_RE = '\\ATC([0-9])\\Z'
    x.CHANNEL_RE = '\\ATC([0-9])_([A-Z])\\Z'
    x.CLOCK_CONFIG = lambda config: 'static const int ClockPrescaleDivide = {};'.format(config.get_int_constant('PrescaleDivide'))
    x.INTERRUPT_TIMER_EXPR = lambda it, clearance_extra: 'AvrClockInterruptTimerService<AvrClockTcChannel{}{}{}>'.format(it['tc'], it['channel'], clearance_extra)
    x.INTERRUPT_TIMER_ISR = lambda it, user: 'AMBRO_AVR_CLOCK_INTERRUPT_TIMER_ISRS({}, {}, {}, MyContext())'.format(it['tc'], it['channel'], user)
    x.TIMER_EXPR = lambda tc: 'AvrClockTcSpec<AvrClockTc{}>'.format(tc)
    x.CLOCK_ISR = lambda clock: 'AMBRO_AVR_CLOCK_ISRS({}, MyClock, MyContext())'.format(clock['primary_timer'])

def setup_clock(gen, config, key):
    clock_sel = config_common.Selection()
    
    @clock_sel.option('At91Sam3xClock')
    def option(clock):
        return CommonClock(gen, clock, At91Sam3xClockDef)
    
    @clock_sel.option('Mk20Clock')
    def option(clock):
        return CommonClock(gen, clock, Mk20ClockDef)
    
    @clock_sel.option('AvrClock')
    def option(clock):
        return CommonClock(gen, clock, AvrClockDef)
    
    gen.register_singleton_object('clock', config.do_selection(key, clock_sel))

def setup_pins (gen, config, key):
    pin_regexes = [IDENTIFIER_REGEX]
    
    pins_sel = config_common.Selection()
    
    @pins_sel.option('At91SamPins')
    def options(pin_config):
        gen.add_aprinter_include('system/At91SamPins.h')
        pin_regexes.append('\\AAt91SamPin<At91SamPio[A-Z],[0-9]{1,3}>\\Z')
        return TemplateExpr('At91SamPins', ['MyContext', 'Program'])
    
    @pins_sel.option('Mk20Pins')
    def options(pin_config):
        gen.add_aprinter_include('system/Mk20Pins.h')
        pin_regexes.append('\\AMk20Pin<Mk20Port[A-Z],[0-9]{1,3}>\\Z')
        return TemplateExpr('Mk20Pins', ['MyContext', 'Program'])
    
    @pins_sel.option('AvrPins')
    def options(pin_config):
        gen.add_aprinter_include('system/AvrPins.h')
        pin_regexes.append('\\AAvrPin<AvrPort[A-Z],[0-9]{1,3}>\\Z')
        return TemplateExpr('AvrPins', ['MyContext', 'Program'])
    
    gen.add_subst('Pins', config.do_selection(key, pins_sel), indent=0)
    
    gen.register_singleton_object('pin_regexes', pin_regexes)

def get_pin (gen, config, key):
    pin = config.get_string(key)
    pin_regexes = gen.get_singleton_object('pin_regexes')
    if not any(re.match(pin_regex, pin) for pin_regex in pin_regexes):
        config.key_path(key).error('Invalid pin value.')
    return pin

def setup_watchdog (gen, config, key, user):
    watchdog_sel = config_common.Selection()
    
    @watchdog_sel.option('At91SamWatchdog')
    def option(watchdog):
        gen.add_aprinter_include('system/At91SamWatchdog.h')
        return TemplateExpr('At91SamWatchdogService', [
            watchdog.get_int('Wdv')
        ])
    
    @watchdog_sel.option('Mk20Watchdog')
    def option(watchdog):
        gen.add_aprinter_include('system/Mk20Watchdog.h')
        gen.add_isr('AMBRO_MK20_WATCHDOG_GLOBAL({})'.format(user))
        return TemplateExpr('Mk20WatchdogService', [
            watchdog.get_int('Toval'),
            watchdog.get_int('Prescval'),
        ])
    
    @watchdog_sel.option('AvrWatchdog')
    def option(watchdog):
        wdto = watchdog.get_string('Timeout')
        if not re.match('\\AWDTO_[0-9A-Z]{1,10}\\Z', wdto):
            watchdog.key_path('Timeout').error('Incorrect value.')
        
        gen.add_aprinter_include('system/AvrWatchdog.h')
        gen.add_isr('AMBRO_AVR_WATCHDOG_GLOBAL')
        return TemplateExpr('AvrWatchdogService', [wdto])
    
    gen.add_subst('Watchdog', config.do_selection(key, watchdog_sel))

def setup_adc (gen, config, key):
    adc_sel = config_common.Selection()
    
    @adc_sel.option('At91SamAdc')
    def option(adc_config):
        gen.add_aprinter_include('system/At91SamAdc.h')
        gen.add_float_constant('AdcFreq', adc_config.get_float('freq'))
        gen.add_float_constant('AdcAvgInterval', adc_config.get_float('avg_interval'))
        gen.add_int_constant('uint16', 'AdcSmoothing', max(0, min(65535, int(adc_config.get_float('smoothing') * 65536.0))))
        gen.add_isr('AMBRO_AT91SAM_ADC_GLOBAL(MyAdc, MyContext())')
        
        return {
            'value_func': lambda: TemplateExpr('At91SamAdc', [
                'MyContext', 'Program', 'AdcPins',
                TemplateExpr('At91SamAdcParams', [
                    'AdcFreq',
                    adc_config.get_int('startup'),
                    adc_config.get_int('settling'),
                    adc_config.get_int('tracking'),
                    adc_config.get_int('transfer'),
                    'At91SamAdcAvgParams<AdcAvgInterval>'
                ])
            ]),
            'pin_func': lambda pin: 'At91SamAdcSmoothPin<{}, AdcSmoothing>'.format(pin)
        }
    
    @adc_sel.option('Mk20Adc')
    def option(adc_config):
        gen.add_aprinter_include('system/Mk20Adc.h')
        gen.add_int_constant('int32', 'AdcADiv', adc_config.get_int('AdcADiv'))
        gen.add_isr('AMBRO_MK20_ADC_ISRS(MyAdc, MyContext())')
        
        return {
            'value_func': lambda: TemplateExpr('Mk20Adc', ['MyContext', 'Program', 'AdcPins', 'AdcADiv']),
            'pin_func': lambda pin: pin
        }
    
    @adc_sel.option('AvrAdc')
    def option(adc_config):
        gen.add_aprinter_include('system/AvrAdc.h')
        gen.add_int_constant('int32', 'AdcRefSel', adc_config.get_int('RefSel'))
        gen.add_int_constant('int32', 'AdcPrescaler', adc_config.get_int('Prescaler'))
        gen.add_isr('AMBRO_AVR_ADC_ISRS(MyAdc, MyContext())')
        
        return {
            'value_func': lambda: TemplateExpr('AvrAdc', ['MyContext', 'Program', 'AdcPins', 'AdcRefSel', 'AdcPrescaler']),
            'pin_func': lambda pin: pin
        }
    
    result = config.do_selection(key, adc_sel)
    
    gen.register_singleton_object('adc_pins', [])
    
    def finalize():
        adc_pins = gen.get_singleton_object('adc_pins')
        gen.add_subst('Adc', result['value_func'](), indent=0)
        gen.add_subst('AdcPins', 'using AdcPins = {};'.format(TemplateList([result['pin_func'](pin) for pin in adc_pins]).build(0)))
    
    gen.add_finalize_action(finalize)

def use_digital_input (gen, config, key):
    di = gen.get_object('digital_input', config, key)
    return '{}, {}'.format(get_pin(gen, di, 'Pin'), di.get_identifier('InputMode'))

def use_analog_input (gen, config, key):
    ai = gen.get_object('analog_input', config, key)
    pin = get_pin(gen, ai, 'Pin')
    gen.get_singleton_object('adc_pins').append(pin)
    return pin

def use_interrupt_timer (gen, config, key, user, clearance=None):
    for it_config in config.enter_config(key):
        return gen.get_singleton_object('clock').add_interrupt_timer(it_config.get_string('oc_unit'), user, clearance, it_config.path())

def use_pwm_output (gen, config, key, user, username):
    pwm_output = gen.get_object('pwm_output', config, key)
    
    backend_sel = config_common.Selection()
    
    @backend_sel.option('SoftPwm')
    def option(backend):
        gen.add_aprinter_include('printer/pwm/SoftPwm.h')
        return TemplateExpr('SoftPwmService', [
            get_pin(gen, backend, 'OutputPin'),
            backend.get_bool('OutputInvert'),
            gen.add_float_constant('{}PulseInterval'.format(username), backend.get_float('PulseInterval')),
            use_interrupt_timer(gen, backend, 'Timer', '{}::TheTimer'.format(user))
        ])
    
    return pwm_output.do_selection('Backend', backend_sel)

def use_spi (gen, config, key, user):
    spi_sel = config_common.Selection()
    
    @spi_sel.option('At91SamSpi')
    def option(spi_config):
        gen.add_aprinter_include('system/At91SamSpi.h')
        devices = {
            'At91Sam3xSpiDevice':'AMBRO_AT91SAM3X_SPI_GLOBAL',
            'At91Sam3uSpiDevice':'AMBRO_AT91SAM3U_SPI_GLOBAL'
        }
        dev = spi_config.get_identifier('Device')
        if dev not in devices:
            spi_config.path().error('Incorrect SPI device.')
        gen.add_isr('{}({}, MyContext())'.format(devices[dev], user))
        return TemplateExpr('At91SamSpiService', [dev])
    
    @spi_sel.option('AvrSpi')
    def option(spi_config):
        gen.add_aprinter_include('system/AvrSpi.h')
        gen.add_isr('AMBRO_AVR_SPI_ISRS({}, MyContext())'.format(user))
        return TemplateExpr('AvrSpiService', [spi_config.get_int('SpeedDiv')])
    
    return config.do_selection(key, spi_sel)

def use_i2c (gen, config, key, user, username):
    i2c_sel = config_common.Selection()
    
    @i2c_sel.option('At91SamI2c')
    def option(i2c_config):
        gen.add_aprinter_include('system/At91SamI2c.h')
        devices = {
            'At91SamI2cDevice1':1,
        }
        dev = i2c_config.get_identifier('Device')
        if dev not in devices:
            i2c_config.path().error('Incorrect I2C device.')
        gen.add_isr('AMBRO_AT91SAM_I2C_GLOBAL({}, {}, MyContext())'.format(devices[dev], user))
        return 'At91SamI2cService<{}, {}, {}>'.format(
            dev,
            i2c_config.get_int('Ckdiv'),
            gen.add_float_constant('{}I2cFreq'.format(username), i2c_config.get_float('I2cFreq'))
        )
    
    return config.do_selection(key, i2c_sel)

def use_eeprom(gen, config, key, user):
    eeprom_sel = config_common.Selection()
    
    @eeprom_sel.option('I2cEeprom')
    def option(eeprom):
        gen.add_aprinter_include('devices/I2cEeprom.h')
        return TemplateExpr('I2cEepromService', [use_i2c(gen, eeprom, 'I2c', '{}::GetI2c'.format(user), 'ConfigEeprom'), eeprom.get_int('I2cAddr'), eeprom.get_int('Size'), eeprom.get_int('BlockSize'), gen.add_float_constant('ConfigEepromWriteTimeout', eeprom.get_float('WriteTimeout'))])
    
    @eeprom_sel.option('TeensyEeprom')
    def option(eeprom):
        gen.add_aprinter_include('system/TeensyEeprom.h')
        return TemplateExpr('TeensyEepromService', [eeprom.get_int('Size'), eeprom.get_int('FakeBlockSize')])
    
    @eeprom_sel.option('AvrEeprom')
    def option(eeprom):
        gen.add_aprinter_include('system/AvrEeprom.h')
        gen.add_isr('AMBRO_AVR_EEPROM_ISRS({}, MyContext())'.format(user))
        return TemplateExpr('AvrEepromService', [eeprom.get_int('FakeBlockSize')])
    
    return config.do_selection(key, eeprom_sel)

def use_serial(gen, config, key, user):
    serial_sel = config_common.Selection()
    
    @serial_sel.option('AsfUsbSerial')
    def option(serial_service):
        gen.add_aprinter_include('system/AsfUsbSerial.h')
        gen.add_init_call(0, 'udc_start();')
        return 'AsfUsbSerialService'
    
    @serial_sel.option('At91Sam3xSerial')
    def option(serial_service):
        gen.add_aprinter_include('system/At91Sam3xSerial.h')
        gen.add_aprinter_include('system/NewlibDebugWrite.h')
        gen.add_isr('AMBRO_AT91SAM3X_SERIAL_GLOBAL({}, MyContext())'.format(user))
        gen.add_global_code(0, 'APRINTER_SETUP_NEWLIB_DEBUG_WRITE(At91Sam3xSerial_DebugWrite<{}>, MyContext())'.format(user))
        return 'At91Sam3xSerialService'
    
    @serial_sel.option('TeensyUsbSerial')
    def option(serial_service):
        gen.add_aprinter_include('system/TeensyUsbSerial.h')
        gen.add_global_code(0, 'extern "C" { void usb_init (void); }')
        gen.add_init_call(0, 'usb_init();')
        return 'TeensyUsbSerialService'
    
    @serial_sel.option('AvrSerial')
    def option(serial_service):
        gen.add_aprinter_include('system/AvrSerial.h')
        gen.add_aprinter_include('system/AvrDebugWrite.h')
        gen.add_isr('AMBRO_AVR_SERIAL_ISRS({}, MyContext())'.format(user))
        gen.add_global_code(0, 'APRINTER_SETUP_AVR_DEBUG_WRITE(AvrSerial_DebugPutChar<{}>, MyContext())'.format(user))
        gen.add_init_call(-2, 'aprinter_init_avr_debug_write();')
        return TemplateExpr('AvrSerialService', [serial_service.get_bool('DoubleSpeed')])
    
    return config.do_selection(key, serial_sel)

def use_sdcard(gen, config, key, user):
    sd_service_sel = config_common.Selection()

    @sd_service_sel.option('SpiSdCard')
    def option(spi_sd):
        gen.add_aprinter_include('devices/SpiSdCard.h')
        return TemplateExpr('SpiSdCardService', [
            get_pin(gen, spi_sd, 'SsPin'),
            use_spi(gen, spi_sd, 'SpiService', '{}::GetSpi'.format(user)),
        ])

    return config.do_selection(key, sd_service_sel)

def use_config_manager(gen, config, key, user):
    config_manager_sel = config_common.Selection()
    
    @config_manager_sel.option('ConstantConfigManager')
    def option(config_manager):
        gen.add_aprinter_include('printer/config_manager/ConstantConfigManager.h')
        return 'ConstantConfigManagerService'
    
    @config_manager_sel.option('RuntimeConfigManager')
    def option(config_manager):
        gen.add_aprinter_include('printer/config_manager/RuntimeConfigManager.h')
        
        config_store_sel = config_common.Selection()
        
        @config_store_sel.option('NoStore')
        def option(config_store):
            return 'RuntimeConfigManagerNoStoreService'
        
        @config_store_sel.option('EepromConfigStore')
        def option(config_store):
            gen.add_aprinter_include('printer/config_store/EepromConfigStore.h')
            
            return TemplateExpr('EepromConfigStoreService', [
                use_eeprom(gen, config_store, 'Eeprom', '{}::GetStore<>::GetEeprom'.format(user)),
                config_store.get_int('StartBlock'),
                config_store.get_int('EndBlock'),
            ])
        
        return TemplateExpr('RuntimeConfigManagerService', [config_manager.do_selection('ConfigStore', config_store_sel)])
    
    return config.do_selection(key, config_manager_sel)

def generate(config_root_data, cfg_name, main_template):
    gen = GenState()
    
    for config_root in config_reader.start(config_root_data, config_reader_class=GenConfigReader):
        if cfg_name is None:
            cfg_name = config_root.get_string('selected_config')
        
        for config in config_root.enter_elem_by_id('configurations', 'name', cfg_name):
            board_id = config.get_string('board_id')
            
            for board_data in config_root.enter_elem_by_id('boards', 'identifier', board_id):
                board_for_build = board_data.get_string('board_for_build')
                if not re.match('\\A[a-zA-Z0-9_]{1,128}\\Z', board_for_build):
                    board_data.key_path('board_for_build').error('Incorrect format.')
                gen.add_subst('BoardForBuild', board_for_build)
                
                output_type = board_data.get_string('output_type')
                if output_type not in ('hex', 'bin'):
                    board_data.key_path('output_type').error('Incorrect value.')
                
                setup_platform(gen, board_data, 'platform')
                
                for platform in board_data.enter_config('platform'):
                    setup_clock(gen, platform, 'clock')
                    setup_pins(gen, platform, 'pins')
                    setup_watchdog(gen, platform, 'watchdog', 'MyPrinter::GetWatchdog')
                    setup_adc(gen, platform, 'adc')
                
                for helper_name in board_data.get_list(config_reader.ConfigTypeString(), 'board_helper_includes', max_count=20):
                    if not re.match('\\A[a-zA-Z0-9_]{1,128}\\Z', helper_name):
                        board_data.key_path('board_helper_includes').error('Invalid helper name.')
                    gen.add_aprinter_include('board/{}.h'.format(helper_name))
                
                gen.register_objects('digital_input', board_data, 'digital_inputs')
                gen.register_objects('stepper_port', board_data, 'stepper_ports')
                gen.register_objects('analog_input', board_data, 'analog_inputs')
                gen.register_objects('pwm_output', board_data, 'pwm_outputs')
                
                gen.add_subst('LedPin', get_pin(gen, board_data, 'LedPin'))
                gen.add_subst('EventChannelTimer', use_interrupt_timer(gen, board_data, 'EventChannelTimer', user='MyPrinter::GetEventChannelTimer', clearance='EventChannelTimerClearance'))
                
                for performance in board_data.enter_config('performance'):
                    gen.add_subst('AxisDriverPrecisionParams', performance.get_identifier('AxisDriverPrecisionParams'))
                    gen.add_float_constant('EventChannelTimerClearance', performance.get_float('EventChannelTimerClearance'))
                    gen.add_float_config('MaxStepsPerCycle', performance.get_float('MaxStepsPerCycle'))
                    gen.add_subst('StepperSegmentBufferSize', performance.get_int_constant('StepperSegmentBufferSize'))
                    gen.add_subst('EventChannelBufferSize', performance.get_int_constant('EventChannelBufferSize'))
                    gen.add_subst('LookaheadBufferSize', performance.get_int_constant('LookaheadBufferSize'))
                    gen.add_subst('LookaheadCommitCount', performance.get_int_constant('LookaheadCommitCount'))
                    gen.add_subst('FpType', performance.get_identifier('FpType', lambda x: x in ('float', 'double')))
                
                for serial in board_data.enter_config('serial'):
                    gen.add_subst('SerialBaudRate', serial.get_int_constant('BaudRate'))
                    gen.add_subst('SerialRecvBufferSizeExp', serial.get_int_constant('RecvBufferSizeExp'))
                    gen.add_subst('SerialSendBufferSizeExp', serial.get_int_constant('SendBufferSizeExp'))
                    gen.add_subst('SerialGcodeMaxParts', serial.get_int_constant('GcodeMaxParts'))
                    gen.add_subst('SerialService', use_serial(gen, serial, 'Service', 'MyPrinter::GetSerial'))
                
                sdcard_sel = config_common.Selection()
                
                @sdcard_sel.option('NoSdCard')
                def option(sdcard):
                    return 'PrinterMainNoSdCardParams'
                
                @sdcard_sel.option('SdCard')
                def option(sdcard):
                    gcode_parser_sel = config_common.Selection()
                    
                    @gcode_parser_sel.option('TextGcodeParser')
                    def option(parser):
                        return 'FileGcodeParser, GcodeParserParams<{}>'.format(parser.get_int('MaxParts'))
                    
                    @gcode_parser_sel.option('BinaryGcodeParser')
                    def option(parser):
                        return 'BinaryGcodeParser, BinaryGcodeParserParams<{}>'.format(parser.get_int('MaxParts'))
                    
                    return TemplateExpr('PrinterMainSdCardParams', [
                        use_sdcard(gen, sdcard, 'SdCardService', 'MyPrinter::GetSdCard<>'),
                        sdcard.do_selection('GcodeParser', gcode_parser_sel),
                        sdcard.get_int('BufferBaseSize'),
                        sdcard.get_int('MaxCommandSize'),
                    ])
                
                gen.add_subst('SdCard', board_data.do_selection('sdcard', sdcard_sel), indent=1)
                
                gen.add_subst('ConfigManager', use_config_manager(gen, board_data, 'config_manager', 'MyPrinter::GetConfigManager'))
            
            gen.add_float_config('InactiveTime', config.get_float('InactiveTime'))
            
            for advanced in config.enter_config('advanced'):
                gen.add_float_constant('LedBlinkInterval', advanced.get_float('LedBlinkInterval'))
                gen.add_float_config('ForceTimeout', advanced.get_float('ForceTimeout'))
            
            probe_sel = config_common.Selection()
            
            @probe_sel.option('NoProbe')
            def option(probe):
                return 'PrinterMainNoProbeParams'
            
            @probe_sel.option('Probe')
            def option(probe):
                gen.add_float_config('ProbeOffsetX', probe.get_float('OffsetX'))
                gen.add_float_config('ProbeOffsetY', probe.get_float('OffsetY'))
                gen.add_float_config('ProbeStartHeight', probe.get_float('StartHeight'))
                gen.add_float_config('ProbeLowHeight', probe.get_float('LowHeight'))
                gen.add_float_config('ProbeRetractDist', probe.get_float('RetractDist'))
                gen.add_float_config('ProbeMoveSpeed', probe.get_float('MoveSpeed'))
                gen.add_float_config('ProbeFastSpeed', probe.get_float('FastSpeed'))
                gen.add_float_config('ProbeRetractSpeed', probe.get_float('RetractSpeed'))
                gen.add_float_config('ProbeSlowSpeed', probe.get_float('SlowSpeed'))
                
                point_list = []
                for (i, point) in enumerate(probe.iter_list_config('ProbePoints', max_count=20)):
                    p = (point.get_float('X'), point.get_float('Y'))
                    gen.add_float_config('ProbeP{}X'.format(i+1), p[0])
                    gen.add_float_config('ProbeP{}Y'.format(i+1), p[1])
                    point_list.append(p)
                
                return TemplateExpr('PrinterMainProbeParams', [
                    'MakeTypeList<WrapInt<\'X\'>, WrapInt<\'Y\'>>',
                    '\'Z\'',
                    use_digital_input(gen, probe, 'ProbePin'),
                    probe.get_bool_constant('InvertInput'),
                    'MakeTypeList<ProbeOffsetX, ProbeOffsetY>',
                    'ProbeStartHeight',
                    'ProbeLowHeight',
                    'ProbeRetractDist',
                    'ProbeMoveSpeed',
                    'ProbeFastSpeed',
                    'ProbeRetractSpeed',
                    'ProbeSlowSpeed',
                    TemplateList(['MakeTypeList<ProbeP{}X, ProbeP{}Y>'.format(i+1, i+1) for i in range(len(point_list))])
                ])
            
            gen.add_subst('Probe', config.do_selection('probe', probe_sel), indent=1)
            
            def stepper_cb(stepper, stepper_index):
                name = stepper.get_id_char('Name')
                
                homing_sel = config_common.Selection()
                
                @homing_sel.option('no_homing')
                def option(homing):
                    return 'PrinterMainNoHomingParams'
                
                @homing_sel.option('homing')
                def option(homing):
                    gen.add_aprinter_include('printer/AxisHomer.h')
                    
                    return TemplateExpr('PrinterMainHomingParams', [
                        gen.add_bool_config('{}HomeDir'.format(name), homing.get_bool('HomeDir')),
                        TemplateExpr('AxisHomerService', [
                            use_digital_input(gen, homing, 'HomeEndstopInput'),
                            gen.add_bool_config('{}HomeEndInvert'.format(name), homing.get_bool('HomeEndInvert')),
                            gen.add_float_config('{}HomeFastMaxDist'.format(name), homing.get_float('HomeFastMaxDist')),
                            gen.add_float_config('{}HomeRetractDist'.format(name), homing.get_float('HomeRetractDist')),
                            gen.add_float_config('{}HomeSlowMaxDist'.format(name), homing.get_float('HomeSlowMaxDist')),
                            gen.add_float_config('{}HomeFastSpeed'.format(name), homing.get_float('HomeFastSpeed')),
                            gen.add_float_config('{}HomeRetractSpeed'.format(name), homing.get_float('HomeRetractSpeed')),
                            gen.add_float_config('{}HomeSlowSpeed'.format(name), homing.get_float('HomeSlowSpeed')),
                        ])
                    ])
                
                stepper_port = gen.get_object('stepper_port', stepper, 'stepper_port')
                
                gen.add_aprinter_include('driver/AxisDriver.h')
                
                return TemplateExpr('PrinterMainAxisParams', [
                    TemplateChar(name),
                    get_pin(gen, stepper_port, 'DirPin'),
                    get_pin(gen, stepper_port, 'StepPin'),
                    get_pin(gen, stepper_port, 'EnablePin'),
                    gen.add_bool_config('{}InvertDir'.format(name), stepper.get_bool('InvertDir')),
                    gen.add_float_config('{}StepsPerUnit'.format(name), stepper.get_float('StepsPerUnit')),
                    gen.add_float_config('{}MinPos'.format(name), stepper.get_float('MinPos')),
                    gen.add_float_config('{}MaxPos'.format(name), stepper.get_float('MaxPos')),
                    gen.add_float_config('{}MaxSpeed'.format(name), stepper.get_float('MaxSpeed')),
                    gen.add_float_config('{}MaxAccel'.format(name), stepper.get_float('MaxAccel')),
                    gen.add_float_config('{}DistanceFactor'.format(name), stepper.get_float('DistanceFactor')),
                    gen.add_float_config('{}CorneringDistance'.format(name), stepper.get_float('CorneringDistance')),
                    stepper.do_selection('homing', homing_sel),
                    stepper.get_bool('EnableCartesianSpeedLimit'),
                    32,
                    TemplateExpr('AxisDriverService', [
                        use_interrupt_timer(gen, stepper_port, 'StepperTimer', user='MyPrinter::GetAxisTimer<{}>'.format(stepper_index)),
                        'TheAxisDriverPrecisionParams'
                    ]),
                    'PrinterMainNoMicroStepParams'
                ])
            
            gen.add_subst('Steppers', config.do_list('steppers', stepper_cb, max_count=15), indent=1)
            
            def heater_cb(heater, heater_index):
                name = heater.get_id_char('Name')
                
                for conversion in heater.enter_config('conversion'):
                    gen.add_aprinter_include('printer/thermistor/GenericThermistor.h')
                    thermistor = TemplateExpr('GenericThermistorService', [
                        gen.add_float_config('{}HeaterTempResistorR'.format(name), conversion.get_float('ResistorR')),
                        gen.add_float_config('{}HeaterTempR0'.format(name), conversion.get_float('R0')),
                        gen.add_float_config('{}HeaterTempBeta'.format(name), conversion.get_float('Beta')),
                        gen.add_float_config('{}HeaterTempMinTemp'.format(name), conversion.get_float('MinTemp')),
                        gen.add_float_config('{}HeaterTempMaxTemp'.format(name), conversion.get_float('MaxTemp')),
                    ])
                
                for control in heater.enter_config('control'):
                    gen.add_aprinter_include('printer/temp_control/PidControl.h')
                    control_interval = control.get_float('ControlInterval')
                    control_service = TemplateExpr('PidControlService', [
                        gen.add_float_config('{}HeaterPidP'.format(name), control.get_float('PidP')),
                        gen.add_float_config('{}HeaterPidI'.format(name), control.get_float('PidI')),
                        gen.add_float_config('{}HeaterPidD'.format(name), control.get_float('PidD')),
                        gen.add_float_config('{}HeaterPidIStateMin'.format(name), control.get_float('PidIStateMin')),
                        gen.add_float_config('{}HeaterPidIStateMax'.format(name), control.get_float('PidIStateMax')),
                        gen.add_float_config('{}HeaterPidDHistory'.format(name), control.get_float('PidDHistory')),
                    ])
                
                for observer in heater.enter_config('observer'):
                    gen.add_aprinter_include('printer/TemperatureObserver.h')
                    observer_service = TemplateExpr('TemperatureObserverService', [
                        gen.add_float_config('{}HeaterObserverInterval'.format(name), observer.get_float('ObserverInterval')),
                        gen.add_float_config('{}HeaterObserverTolerance'.format(name), observer.get_float('ObserverTolerance')),
                        gen.add_float_config('{}HeaterObserverMinTime'.format(name), observer.get_float('ObserverMinTime')),
                    ])
                
                return TemplateExpr('PrinterMainHeaterParams', [
                    TemplateChar(name),
                    heater.get_int('SetMCommand'),
                    heater.get_int('WaitMCommand'),
                    use_analog_input(gen, heater, 'ThermistorInput'),
                    thermistor,
                    gen.add_float_config('{}HeaterMinSafeTemp'.format(name), heater.get_float('MinSafeTemp')),
                    gen.add_float_config('{}HeaterMaxSafeTemp'.format(name), heater.get_float('MaxSafeTemp')),
                    gen.add_float_config('{}HeaterControlInterval'.format(name), control_interval),
                    control_service,
                    observer_service,
                    use_pwm_output(gen, heater, 'pwm_output', 'MyPrinter::GetHeaterPwm<{}>'.format(heater_index), '{}Heater'.format(name))
                ])
            
            gen.add_subst('Heaters', config.do_list('heaters', heater_cb, max_count=15), indent=1)
            
            def fan_cb(fan, fan_index):
                name = fan.get_id_char('Name')
                
                return TemplateExpr('PrinterMainFanParams', [
                    fan.get_int('SetMCommand'),
                    fan.get_int('OffMCommand'),
                    'FanSpeedMultiply',
                    use_pwm_output(gen, fan, 'pwm_output', 'MyPrinter::GetFanPwm<{}>'.format(fan_index), '{}Fan'.format(name))
                ])
            
            gen.add_subst('Fans', config.do_list('fans', fan_cb, max_count=15), indent=1)
    
    gen.finalize()
    
    return {
        'main_source': config_common.RichTemplate(main_template).substitute(gen.get_subst()),
        'board_for_build': board_for_build,
        'output_type': output_type
    }

def main():
    # Parse arguments.
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--cfg-name')
    parser.add_argument('--output', required=True)
    parser.add_argument('--nix', action='store_true')
    parser.add_argument('--nix-dir')
    args = parser.parse_args()
    
    # Determine source dir.
    src_dir = config_common.file_dir(__file__)
    
    # Read main template file.
    main_template = config_common.read_file(os.path.join(src_dir, 'main_template.cpp'))
    
    # Generate.
    with config_common.use_input_file(args.config) as config_f:
        result = generate(json.load(config_f), args.cfg_name, main_template)
    
    # Write results.
    with config_common.use_output_file(args.output) as output_f:
        if args.nix:
            nix = 'with import (builtins.toPath {}); aprinterFunc {{ boardName = {}; buildName = "aprinter"; desiredOutputs = [{}]; mainText = {}; }}'.format(
                config_common.escape_string_for_nix(args.nix_dir),
                config_common.escape_string_for_nix(result['board_for_build']),
                config_common.escape_string_for_nix(result['output_type']),
                config_common.escape_string_for_nix(result['main_source'])
            )
            output_f.write(nix)
        else:
            output_f.write(result['main_source'])
    
main()
