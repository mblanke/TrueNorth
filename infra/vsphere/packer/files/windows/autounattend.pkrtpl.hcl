<?xml version="1.0" encoding="utf-8"?>
<!-- TrueNorth Range — shared Windows Autounattend, rendered per image by templatefile().
     Delivered to Setup via a second CD (cd_content / cd_label="PROVISION"). Setup reads
     autounattend.xml from the root of any attached removable media.

     Proven 4-partition GPT/UEFI layout (WinRE 300MB / EFI 100MB FAT32 / MSR 16MB /
     Windows). The lab's lessons-learned: a 1-partition layout left no ESP ("error
     selecting this partition") and a naive 3-partition layout hit a BFSVC bootmgfw copy
     failure on the Server 2025 / Win11 24H2+ new-setup engine. This layout is what the
     widely used community Windows Packer templates use and installs on the classic
     (2022/2019/2016/10) engine reliably; it is the best-effort path for the new engine. -->
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>Primary</Type><Size>300</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>EFI</Type><Size>100</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>4</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>NTFS</Format><Label>WinRE</Label><TypeID>DE94BBA4-06D1-4D40-A16A-BFD50179D6AC</TypeID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>3</Order><PartitionID>3</PartitionID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>4</Order><PartitionID>4</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>
        </Disk>
        <WillShowUI>OnError</WillShowUI>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add"><Key>/IMAGE/NAME</Key><Value>${image_name}</Value></MetaData>
          </InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>4</PartitionID></InstallTo>
          <WillShowUI>OnError</WillShowUI>
        </OSImage>
      </ImageInstall>
%{ if length(pvscsi_driver_paths) > 0 }
      <!-- pvscsi controller driver, loaded in WinPE off the mounted VMware Tools ISO.
           Tools-ISO layout varies by version, so several candidate drive letters and
           relative paths are listed; Setup uses the first that matches. -->
      <DriverPaths>
%{ for p in pvscsi_driver_paths ~}
        <PathAndCredentials wcm:action="add" wcm:keyValue="${p.key}"><Path>${p.path}</Path></PathAndCredentials>
%{ endfor ~}
      </DriverPaths>
%{ endif }
%{ if labconfig }
      <!-- Win11 / new-setup lab bypasses (no vTPM / Secure Boot / RAM / CPU requirement). -->
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>2</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>3</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>4</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassCPUCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>5</Order><Path>reg add HKLM\System\Setup\LabConfig /v BypassStorageCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
%{ endif }
      <UserData>
        <AcceptEula>true</AcceptEula>
        <FullName>TrueNorth</FullName>
        <Organization>TrueNorth Range</Organization>
%{ if product_key != "" }
        <ProductKey><Key>${product_key}</Key><WillShowUI>OnError</WillShowUI></ProductKey>
%{ else }
        <ProductKey><WillShowUI>Never</WillShowUI></ProductKey>
%{ endif }
      </UserData>
    </component>
  </settings>

  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <ComputerName>${computer_name}</ComputerName>
      <TimeZone>UTC</TimeZone>
    </component>
%{ if bypass_nro }
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE" /v BypassNRO /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
    </component>
%{ endif }
  </settings>

  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword>
          <Value>${admin_password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled>
        <LogonCount>2</LogonCount>
        <Password><Value>${admin_password}</Value><PlainText>true</PlainText></Password>
        <Username>Administrator</Username>
      </AutoLogon>
      <TimeZone>UTC</TimeZone>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-ExecutionPolicy Bypass -Scope LocalMachine -Force"</CommandLine>
          <Description>Set execution policy</Description>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>2</Order>
          <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "winrm quickconfig -q"</CommandLine>
          <Description>Enable WinRM</Description>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>3</Order>
          <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "winrm set winrm/config/service '@{AllowUnencrypted=\"true\"}'"</CommandLine>
          <Description>Allow unencrypted WinRM</Description>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>4</Order>
          <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "winrm set winrm/config/service/auth '@{Basic=\"true\"}'"</CommandLine>
          <Description>Enable WinRM basic auth</Description>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>5</Order>
          <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "winrm set winrm/config '@{MaxTimeoutms=\"1800000\"}'"</CommandLine>
          <Description>WinRM timeout</Description>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>6</Order>
          <CommandLine>netsh advfirewall firewall add rule name="WinRM-HTTP" dir=in localport=5985 protocol=tcp action=allow</CommandLine>
          <Description>Open WinRM firewall port</Description>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
