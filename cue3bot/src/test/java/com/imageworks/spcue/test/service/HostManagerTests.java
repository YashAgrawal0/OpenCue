
/*
 * Copyright (c) 2018 Sony Pictures Imageworks Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */



package com.imageworks.spcue.test.service;

import static org.junit.Assert.*;

import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;

import javax.annotation.Resource;

import org.junit.Before;
import org.junit.Test;
import org.springframework.test.annotation.Rollback;
import org.springframework.test.context.junit4.AbstractTransactionalJUnit4SpringContextTests;
import org.springframework.test.context.transaction.TransactionConfiguration;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.test.context.ContextConfiguration;
import org.springframework.test.context.support.AnnotationConfigContextLoader;

import com.imageworks.spcue.config.TestAppConfig;
import com.imageworks.spcue.AllocationDetail;
import com.imageworks.spcue.DispatchFrame;
import com.imageworks.spcue.DispatchHost;
import com.imageworks.spcue.EntityModificationError;
import com.imageworks.spcue.FrameDetail;
import com.imageworks.spcue.Host;
import com.imageworks.spcue.JobDetail;
import com.imageworks.spcue.Owner;
import com.imageworks.spcue.Show;
import com.imageworks.spcue.VirtualProc;
import com.imageworks.spcue.CueIce.HardwareState;
import com.imageworks.spcue.RqdIce.RenderHost;
import com.imageworks.spcue.dao.AllocationDao;
import com.imageworks.spcue.dao.FacilityDao;
import com.imageworks.spcue.dao.FrameDao;
import com.imageworks.spcue.dao.HostDao;
import com.imageworks.spcue.dao.ProcDao;
import com.imageworks.spcue.service.AdminManager;
import com.imageworks.spcue.service.HostManager;
import com.imageworks.spcue.service.JobLauncher;
import com.imageworks.spcue.service.JobManager;
import com.imageworks.spcue.service.JobSpec;
import com.imageworks.spcue.service.OwnerManager;
import com.imageworks.spcue.util.CueUtil;

@Transactional
@ContextConfiguration(classes=TestAppConfig.class, loader=AnnotationConfigContextLoader.class)
@TransactionConfiguration(transactionManager="transactionManager")
public class HostManagerTests extends AbstractTransactionalJUnit4SpringContextTests  {

    @Resource
    AdminManager adminManager;

    @Resource
    HostManager hostManager;

    @Resource
    HostDao hostDao;

    @Resource
    FacilityDao facilityDao;

    @Resource
    FrameDao frameDao;

    @Resource
    ProcDao procDao;

    @Resource
    AllocationDao allocationDao;

    @Resource
    JobManager jobManager;

    @Resource
    JobLauncher jobLauncher;

    @Resource
    OwnerManager ownerManager;

    private static final String HOST_NAME = "alpha1";

    public DispatchHost createHost() {

        RenderHost host = new RenderHost();
        host.name = HOST_NAME;
        host.bootTime = 1192369572;
        host.freeMcp = 7602;
        host.freeMem = 15290520;
        host.freeSwap = 2076;
        host.load = 1;
        host.totalMcp = 19543;
        host.totalMem = (int) CueUtil.GB16;
        host.totalSwap = 2096;
        host.nimbyEnabled = true;
        host.numProcs = 2;
        host.coresPerProc = 400;
        host.tags = new ArrayList<String>();
        host.tags.add("linux");
        host.tags.add("64bit");
        host.state = HardwareState.Up;
        host.facility = "spi";
        host.attributes = new HashMap<String, String>();
        host.attributes.put("freeGpu", "512");
        host.attributes.put("totalGpu", "512");

        hostDao.insertRenderHost(host,
                adminManager.findAllocationDetail("spi", "general"));

        return hostDao.findDispatchHost(HOST_NAME);
    }

    @Before
    public void setTestMode() {
        jobLauncher.testMode = true;
    }

    /**
     * Test that moves a host from one allocation to another.
     */
    @Test
    @Transactional
    @Rollback(true)
    public void setAllocation() {
        Host h = createHost();
        hostManager.setAllocation(h,
                allocationDao.findAllocationDetail("spi", "general"));
    }

    /**
     * This test ensures you can't transfer a host that has a proc
     * assigned to a show without a subscription to the destination
     * allocation.
     */
    @Test(expected=EntityModificationError.class)
    @Transactional
    @Rollback(true)
    public void setBadAllocation() {

        jobLauncher.launch(new File("src/test/resources/conf/jobspec/facility.xml"));
        JobDetail job = jobManager.findJobDetail("pipe-dev.cue-testuser_shell_v1");
        FrameDetail frameDetail = frameDao.findFrameDetail(job, "0001-pass_1");
        DispatchFrame frame = frameDao.getDispatchFrame(frameDetail.id);

        DispatchHost h = createHost();

        AllocationDetail ad =
            allocationDao.findAllocationDetail("spi", "desktop");

        VirtualProc proc = VirtualProc.build(h, frame);
        proc.frameId = frame.id;
        procDao.insertVirtualProc(proc);

        jdbcTemplate.queryForObject(
                "SELECT int_cores FROM subscription WHERE pk_show=? AND pk_alloc=?",
                Integer.class, job.getShowId(), ad.getAllocationId());

        AllocationDetail ad2 = allocationDao.findAllocationDetail("spi", "desktop");
        hostManager.setAllocation(h, ad2);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testGetPrefferedShow() {
        DispatchHost h = createHost();

        Show pshow = adminManager.findShowDetail("pipe");
        Owner o = ownerManager.createOwner("spongebob", pshow);

        ownerManager.takeOwnership(o, h);

        Show show = hostManager.getPreferredShow(h);
        assertEquals(pshow, show);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testisPrefferedShow() {
        DispatchHost h = createHost();

        assertFalse(hostManager.isPreferShow(h));

        Show pshow = adminManager.findShowDetail("pipe");
        Owner o = ownerManager.createOwner("spongebob", pshow);

        ownerManager.takeOwnership(o, h);

        Show show = hostManager.getPreferredShow(h);
        assertEquals(pshow, show);

        assertTrue(hostManager.isPreferShow(h));
    }

}
