/*
 * Copyright (c) 2013 Ambroz Bizjak
 * All rights reserved.
 * 
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 * 
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
 * DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 * (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 * LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
 * ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
 * SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#ifndef AMBROLIB_AXIS_HOMER_H
#define AMBROLIB_AXIS_HOMER_H

#include <stdint.h>

#include <aprinter/meta/WrapFunction.h>
#include <aprinter/meta/MakeTypeList.h>
#include <aprinter/base/Object.h>
#include <aprinter/meta/TupleGet.h>
#include <aprinter/base/DebugObject.h>
#include <aprinter/base/Assert.h>
#include <aprinter/meta/MinMax.h>
#include <aprinter/meta/WrapDouble.h>
#include <aprinter/meta/ExprFixedPoint.h>
#include <aprinter/printer/MotionPlanner.h>
#include <aprinter/printer/Configuration.h>

#include <aprinter/BeginNamespace.h>

template <typename Context, typename Config, typename Params>
class AxisHomerGlobal {
    using CSwitchInvert = decltype(ExprCast<bool>(Config::e(Params::SwitchInvert::i)));
    
public:
    static void init (Context c)
    {
        Context::Pins::template setInput<typename Params::SwitchPin, typename Params::SwitchPinInputMode>(c);
    }
    
    template <typename ThisContext>
    static bool endstop_is_triggered (ThisContext c)
    {
        return (Context::Pins::template get<typename Params::SwitchPin>(c) != APRINTER_CFG(Config, CSwitchInvert, c));
    }
    
    struct Object {};
    
    using ConfigExprs = MakeTypeList<CSwitchInvert>;
};

template <
    typename Context, typename ParentObject, typename Config,
    typename TheGlobal, typename FpType,
    typename TheAxisDriver, int PlannerStepBits, int StepperSegmentBufferSize,
    int MaxLookaheadBufferSize, typename MaxAccel, typename DistConversion,
    typename TimeConversion, typename HomeDir, typename FinishedHandler,
    typename Params
>
class AxisHomer {
public:
    struct Object;
    
private:
    struct PlannerPullHandler;
    struct PlannerFinishedHandler;
    struct PlannerAbortedHandler;
    struct PlannerUnderrunCallback;
    struct PlannerPrestepCallback;
    
    static int const LookaheadBufferSize = MinValue(MaxLookaheadBufferSize, 3);
    static int const LookaheadCommitCount = 1;
    
    using SpeedConversion = decltype(DistConversion() / TimeConversion());
    using AccelConversion = decltype(DistConversion() / (TimeConversion() * TimeConversion()));
    
    using FastSteps = decltype(Config::e(Params::FastMaxDist::i) * DistConversion());
    using RetractSteps = decltype(Config::e(Params::RetractDist::i) * DistConversion());
    using SlowSteps = decltype(Config::e(Params::SlowMaxDist::i) * DistConversion());
    
    using PlannerMaxSpeedRec = APRINTER_FP_CONST_EXPR(0.0);
    using PlannerMaxAccelRec = decltype(ExprRec(MaxAccel() * AccelConversion()));
    using PlannerDistanceFactor = APRINTER_FP_CONST_EXPR(1.0);
    using PlannerCorneringDistance = APRINTER_FP_CONST_EXPR(1.0);
    
    using PlannerAxes = MakeTypeList<MotionPlannerAxisSpec<TheAxisDriver, PlannerStepBits, PlannerDistanceFactor, PlannerCorneringDistance, PlannerMaxSpeedRec, PlannerMaxAccelRec, PlannerPrestepCallback>>;
    using Planner = MotionPlanner<Context, Object, Config, PlannerAxes, StepperSegmentBufferSize, LookaheadBufferSize, LookaheadCommitCount, FpType, PlannerPullHandler, PlannerFinishedHandler, PlannerAbortedHandler, PlannerUnderrunCallback>;
    using PlannerCommand = typename Planner::SplitBuffer;
    
    using TheDebugObject = DebugObject<Context, Object>;
    
    enum {STATE_FAST, STATE_RETRACT, STATE_SLOW, STATE_END};
    
public:
    using StepFixedType = typename Planner::template Axis<0>::StepFixedType;
    
    static void init (Context c)
    {
        auto *o = Object::self(c);
        
        Planner::init(c, true);
        o->m_state = STATE_FAST;
        o->m_command_sent = false;
        
        TheDebugObject::init(c);
    }
    
    static void deinit (Context c)
    {
        auto *o = Object::self(c);
        TheDebugObject::deinit(c);
        
        if (o->m_state != STATE_END) {
            Planner::deinit(c);
        }
    }
    
    using TheAxisDriverConsumer = typename Planner::template TheAxisDriverConsumer<0>;
    
private:
    static void planner_pull_handler (Context c)
    {
        auto *o = Object::self(c);
        TheDebugObject::access(c);
        AMBRO_ASSERT(o->m_state != STATE_END)
        
        if (o->m_command_sent) {
            Planner::waitFinished(c);
            return;
        }
        
        PlannerCommand *cmd = Planner::getBuffer(c);
        auto *axis_cmd = TupleGetElem<0>(cmd->axes.axes());
        FpType max_v_rec;
        switch (o->m_state) {
            case STATE_FAST: {
                axis_cmd->dir = APRINTER_CFG(Config, CHomeDir, c);
                axis_cmd->x = APRINTER_CFG(Config, CFixedStepsFast, c);
                max_v_rec = APRINTER_CFG(Config, CMaxVRecFast, c);
            } break;
            case STATE_RETRACT: {
                axis_cmd->dir = !APRINTER_CFG(Config, CHomeDir, c);
                axis_cmd->x = APRINTER_CFG(Config, CFixedStepsRetract, c);
                max_v_rec = APRINTER_CFG(Config, CMaxVRecRetract, c);
            } break;
            case STATE_SLOW: {
                axis_cmd->dir = APRINTER_CFG(Config, CHomeDir, c);
                axis_cmd->x = APRINTER_CFG(Config, CFixedStepsSlow, c);
                max_v_rec = APRINTER_CFG(Config, CMaxVRecSlow, c);
            } break;
        }
        cmd->axes.rel_max_v_rec = axis_cmd->x.template fpValue<FpType>() * max_v_rec;
        
        if (axis_cmd->x.bitsValue() != 0) {
            Planner::axesCommandDone(c);
        } else {
            Planner::emptyDone(c);
        }
        o->m_command_sent = true;
    }
    
    static void planner_finished_handler (Context c)
    {
        auto *o = Object::self(c);
        TheDebugObject::access(c);
        AMBRO_ASSERT(o->m_state != STATE_END)
        AMBRO_ASSERT(o->m_command_sent)
        
        Planner::deinit(c);
        
        if (o->m_state != STATE_RETRACT) {
            o->m_state = STATE_END;
            return FinishedHandler::call(c, false);
        }
        
        o->m_state++;
        Planner::init(c, true);
        o->m_command_sent = false;
    }
    
    static void planner_aborted_handler (Context c)
    {
        auto *o = Object::self(c);
        TheDebugObject::access(c);
        AMBRO_ASSERT(o->m_state == STATE_FAST || o->m_state == STATE_SLOW)
        
        Planner::deinit(c);
        o->m_state++;
        
        if (o->m_state == STATE_END) {
            return FinishedHandler::call(c, true);
        }
        
        Planner::init(c, o->m_state != STATE_RETRACT);
        o->m_command_sent = false;
    }
    
    static void planner_underrun_callback (Context c)
    {
    }
    
    static bool planner_prestep_callback (typename Planner::template Axis<0>::StepperCommandCallbackContext c)
    {
        return TheGlobal::endstop_is_triggered(c);
    }
    
    using CMaxVRecFast = decltype(ExprCast<FpType>(ExprRec(Config::e(Params::FastSpeed::i) * SpeedConversion())));
    using CMaxVRecRetract = decltype(ExprCast<FpType>(ExprRec(Config::e(Params::RetractSpeed::i) * SpeedConversion())));
    using CMaxVRecSlow = decltype(ExprCast<FpType>(ExprRec(Config::e(Params::SlowSpeed::i) * SpeedConversion())));
    using CFixedStepsFast = decltype(ExprFixedPointImport<StepFixedType>(FastSteps()));
    using CFixedStepsRetract = decltype(ExprFixedPointImport<StepFixedType>(RetractSteps()));
    using CFixedStepsSlow = decltype(ExprFixedPointImport<StepFixedType>(SlowSteps()));
    using CHomeDir = decltype(ExprCast<bool>(HomeDir()));
    
    struct PlannerPullHandler : public AMBRO_WFUNC_TD(&AxisHomer::planner_pull_handler) {};
    struct PlannerFinishedHandler : public AMBRO_WFUNC_TD(&AxisHomer::planner_finished_handler) {};
    struct PlannerAbortedHandler : public AMBRO_WFUNC_TD(&AxisHomer::planner_aborted_handler) {};
    struct PlannerUnderrunCallback : public AMBRO_WFUNC_TD(&AxisHomer::planner_underrun_callback) {};
    struct PlannerPrestepCallback : public AMBRO_WFUNC_TD(&AxisHomer::planner_prestep_callback) {};
    
public:
    using ConfigExprs = MakeTypeList<CMaxVRecFast, CMaxVRecRetract, CMaxVRecSlow, CFixedStepsFast, CFixedStepsRetract, CFixedStepsSlow, CHomeDir>;
    
    struct Object : public ObjBase<AxisHomer, ParentObject, MakeTypeList<
        TheDebugObject,
        Planner
    >> {
        uint8_t m_state;
        bool m_command_sent;
    };
};

template <
    typename TSwitchPin, typename TSwitchPinInputMode, typename TSwitchInvert,
    typename TFastMaxDist, typename TRetractDist, typename TSlowMaxDist,
    typename TFastSpeed, typename TRetractSpeed, typename TSlowSpeed
>
struct AxisHomerService {
    using SwitchPin = TSwitchPin;
    using SwitchPinInputMode = TSwitchPinInputMode;
    using SwitchInvert = TSwitchInvert;
    using FastMaxDist = TFastMaxDist;
    using RetractDist = TRetractDist;
    using SlowMaxDist = TSlowMaxDist;
    using FastSpeed = TFastSpeed;
    using RetractSpeed = TRetractSpeed;
    using SlowSpeed = TSlowSpeed;
    
    template <
        typename Context, typename Config, typename FpType, int PlannerStepBits,
        int StepperSegmentBufferSize, int MaxLookaheadBufferSize, typename MaxAccel,
        typename DistConversion, typename TimeConversion, typename HomeDir
    >
    struct Instance {
        template <typename ParentObject>
        using HomerGlobal = AxisHomerGlobal<Context, Config, AxisHomerService>;
        
        template <typename ParentObject, typename TheGlobal, typename TheAxisDriver, typename FinishedHandler>
        using Homer = AxisHomer<
            Context, ParentObject, Config, TheGlobal, FpType, TheAxisDriver, PlannerStepBits,
            StepperSegmentBufferSize, MaxLookaheadBufferSize, MaxAccel, DistConversion,
            TimeConversion, HomeDir, FinishedHandler, AxisHomerService
        >;
    };
};

#include <aprinter/EndNamespace.h>

#endif
