import QtQuick 2.9
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3

Rectangle {
  id: root
  color: "transparent"
  anchors.fill: parent
  Layout.minimumWidth: 320
  Layout.minimumHeight: 360

  function submitTask() {
    if (HorusCommander.SendTask(taskField.text))
      taskField.clear()
  }

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: 10
    spacing: 8

    RowLayout {
      Layout.fillWidth: true
      spacing: 8

      TextField {
        id: taskField
        objectName: "taskField"
        Layout.fillWidth: true
        placeholderText: qsTr("Enter a task for HORUS")
        selectByMouse: true
        onAccepted: root.submitTask()
      }

      Button {
        id: sendButton
        objectName: "sendButton"
        text: qsTr("Send")
        highlighted: true
        enabled: taskField.text.trim().length > 0
        onClicked: root.submitTask()
      }
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: 6

      Button {
        objectName: "holdButton"
        text: qsTr("HOLD")
        onClicked: HorusCommander.SendEmergency("hold")
      }
      Button {
        objectName: "landButton"
        text: qsTr("LAND")
        onClicked: HorusCommander.SendEmergency("land")
      }
      Button {
        objectName: "rthButton"
        text: qsTr("RTH")
        onClicked: HorusCommander.SendEmergency("rth")
      }
      Button {
        objectName: "newMissionButton"
        text: qsTr("NEW MISSION")
        onClicked: HorusCommander.ResetMission()
      }
    }

    Label {
      text: qsTr("Events")
      font.bold: true
    }

    ScrollView {
      id: eventScroll
      objectName: "eventScroll"
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true

      TextArea {
        id: eventLog
        objectName: "eventLog"
        text: HorusCommander.eventLog
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.Wrap
        onTextChanged: cursorPosition = length
      }
    }

    Rectangle {
      Layout.fillWidth: true
      height: 1
      color: "#666666"
    }

    Label {
      id: statusLine
      objectName: "statusLine"
      Layout.fillWidth: true
      text: qsTr("Status: %1").arg(HorusCommander.status)
      elide: Text.ElideRight
      wrapMode: Text.NoWrap
    }
  }
}
